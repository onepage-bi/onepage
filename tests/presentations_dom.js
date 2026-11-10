/* Headless harness for static/js/presentations.js.
 *
 * The file is written so its top block is pure — no document, no window — and
 * `module.exports` is filled BEFORE the `typeof document === 'undefined'`
 * bail-out. So node can require it directly and drive the four contracts that
 * a browser would otherwise be the only place to check:
 *
 *   1. the order payload is read off the DOM order, not off a model;
 *   2. the upload sends FormData and sets NO Content-Type (the browser must
 *      write the multipart boundary itself — OriginGuard only requires that
 *      the header exist, and a hand-written one corrupts the body);
 *   3. every JSON write sets `Content-Type: application/json` (OriginGuard
 *      rejects a bodied non-GET without it);
 *   4. the show-mode keyboard map — one table shared by the viewer bar and the
 *      slide page, so Space can never mean two different things.
 *
 * Run: node tests/presentations_dom.js   (prints PASS/FAIL lines + ALL PASS)
 */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const FAILURES = [];
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (!cond && extra ? '  <-- ' + extra : ''));
  if (!cond) FAILURES.push(label);
}

const P = require(path.join(ROOT, 'static', 'js', 'presentations.js'));
check('module exports the pure helpers without a DOM',
  P && typeof P.orderIdsFrom === 'function' && typeof P.navFor === 'function');

// ── 1. urls ──────────────────────────────────────────────────────────
check('presApiUrl escapes the id', P.presApiUrl('a b') === '/api/presentations/a%20b',
  P.presApiUrl('a b'));
check('itemUrl targets one slide',
  P.itemUrl('p1', 'i1') === '/api/presentations/p1/items/i1', P.itemUrl('p1', 'i1'));
check('orderUrl / uploadUrl / checkUrl',
  P.orderUrl('p1') === '/api/presentations/p1/order' &&
  P.uploadUrl('p1') === '/api/presentations/p1/upload' &&
  P.checkUrl('p1') === '/api/presentations/p1/check');
check('builderUrl is the page, not the api', P.builderUrl('p1') === '/presentations/p1');

// A report slide is the report's OWN viewer url — presentations are routing.
check('a dashboard slide routes to /d/{id} with pres+i',
  P.slideUrl('p1', { kind: 'dashboard', dashboard_id: 'd9' }, 3) === '/d/d9?pres=p1&i=3',
  P.slideUrl('p1', { kind: 'dashboard', dashboard_id: 'd9' }, 3));
check('an image slide routes to the slide page',
  P.slideUrl('p1', { kind: 'image' }, 2) === '/presentations/p1/slide/2');
check('a dashboard slide with no dashboard_id falls back to the slide page',
  P.slideUrl('p1', { kind: 'dashboard' }, 0) === '/presentations/p1/slide/0');
check('"Show" targets the first slide',
  P.firstSlideUrl('p1', [{ kind: 'image' }, { kind: 'image' }]) === '/presentations/p1/show?i=0');
check('"Show" on an empty deck has no url', P.firstSlideUrl('p1', []) === null);

// ── 2. order payload comes from the DOM order ────────────────────────
function fakeList(ids) {
  const nodes = ids.map(id => ({ dataset: { id: id } }));
  return {
    nodes: nodes,
    querySelectorAll(sel) { this.lastSel = sel; return this.nodes; },
  };
}
const list = fakeList(['c', 'a', 'b']);
check('order payload is the DOM order, top to bottom',
  JSON.stringify(P.orderIdsFrom(list, '.pres-slide')) === '["c","a","b"]',
  JSON.stringify(P.orderIdsFrom(list, '.pres-slide')));
check('order reads the slide rows, not every element', list.lastSel === '.pres-slide');
// After a drag the rows are physically moved, so re-reading gives the new order.
list.nodes.unshift(list.nodes.pop());
check('moving a row changes the payload',
  JSON.stringify(P.orderIdsFrom(list, '.pres-slide')) === '["b","c","a"]');
check('a row with no data-id is skipped',
  JSON.stringify(P.orderIdsFrom(fakeList(['x', undefined, 'y']), '[data-id]')) === '["x","y"]');
check('no list -> empty payload', JSON.stringify(P.orderIdsFrom(null)) === '[]');

// ── 3. request shapes ────────────────────────────────────────────────
const init = P.jsonRequest('PATCH', { note: 'hi' });
check('a JSON write sets Content-Type (OriginGuard)',
  init.headers['Content-Type'] === 'application/json', JSON.stringify(init.headers));
check('a JSON write serializes its body', init.body === '{"note":"hi"}', init.body);
check('a bodiless JSON request sends no body',
  P.jsonRequest('DELETE').body === undefined);

const up = P.uploadRequest({ name: 'shot.png' }, 'note text');
check('the upload is a POST', up.method === 'POST');
check('the upload body is FormData', up.body instanceof FormData, typeof up.body);
check('the upload sets NO Content-Type — the browser writes the boundary',
  up.headers === undefined, JSON.stringify(up.headers));
check('the upload carries the file under `file`', up.body.get('file') !== null);
check('the upload carries the note when there is one', up.body.get('note') === 'note text');
check('no note -> no note field',
  P.uploadRequest({ name: 'x.png' }).body.get('note') === null);

// The wrappers, driven through a fetch stub — this is what the page calls.
const CALLS = [];
global.fetch = function (url, init) { CALLS.push({ url: url, init: init || {} }); return Promise.resolve({ ok: true }); };

P.saveNote('p1', 'i1', 'a note');
let c = CALLS[CALLS.length - 1];
check('note saved by PATCH on the slide',
  c.url === '/api/presentations/p1/items/i1' && c.init.method === 'PATCH', c.url + ' ' + c.init.method);
check('note PATCH carries the JSON content type',
  c.init.headers['Content-Type'] === 'application/json');
check('note PATCH sends the note', c.init.body === '{"note":"a note"}', c.init.body);

P.saveOrder('p1', ['b', 'a']);
c = CALLS[CALLS.length - 1];
check('order saved by PUT …/order with the id list',
  c.url === '/api/presentations/p1/order' && c.init.method === 'PUT' &&
  c.init.body === '{"ids":["b","a"]}', c.url + ' ' + c.init.method + ' ' + c.init.body);

P.addDashboard('p1', 'd9');
c = CALLS[CALLS.length - 1];
check('a report slide is added by POST …/items with kind=dashboard',
  c.url === '/api/presentations/p1/items' && c.init.method === 'POST' &&
  JSON.parse(c.init.body).kind === 'dashboard' &&
  JSON.parse(c.init.body).dashboard_id === 'd9', c.init.body);

P.uploadSlide('p1', { name: 'shot.png' });
c = CALLS[CALLS.length - 1];
check('an upload goes to …/upload as FormData with no headers',
  c.url === '/api/presentations/p1/upload' && c.init.body instanceof FormData &&
  c.init.headers === undefined);

P.saveTitle('p1', 'New name');
c = CALLS[CALLS.length - 1];
check('the title is renamed by PATCH on the presentation',
  c.url === '/api/presentations/p1' && c.init.method === 'PATCH' &&
  c.init.body === '{"title":"New name"}', c.url + ' ' + c.init.body);

P.createPresentation('New presentation');
c = CALLS[CALLS.length - 1];
check('"build a presentation" POSTs to the collection',
  c.url === '/api/presentations' && c.init.method === 'POST');

P.deleteItem('p1', 'i1');
c = CALLS[CALLS.length - 1];
check('a slide is removed by DELETE', c.init.method === 'DELETE' && c.init.body === undefined);

// ── 4. keyboard map ──────────────────────────────────────────────────
const KEYS = [
  ['ArrowRight', 'next'], ['PageDown', 'next'], [' ', 'next'], ['Spacebar', 'next'],
  ['ArrowLeft', 'prev'], ['PageUp', 'prev'],
  ['Escape', 'exit'],
  ['a', null], ['Enter', null], ['ArrowUp', null], ['Tab', null],
];
let keysOk = true, badKey = '';
for (const [key, want] of KEYS) {
  if (P.navFor(key) !== want) { keysOk = false; badKey = key + ' -> ' + P.navFor(key); break; }
}
check('the show-mode keyboard map is exactly next/prev/exit', keysOk, badKey);

// A shortcut must never fire while a note is being typed.
check('typing in a textarea suppresses the shortcuts',
  P.isTyping({ tagName: 'textarea' }) === true);
check('typing in an input suppresses the shortcuts', P.isTyping({ tagName: 'INPUT' }) === true);
check('a contenteditable suppresses the shortcuts',
  P.isTyping({ tagName: 'DIV', isContentEditable: true }) === true);
check('a plain element does not', P.isTyping({ tagName: 'DIV', getAttribute: () => null }) === false);
check('no target does not', P.isTyping(null) === false);

// ── kind labels, shown on every slide row ────────────────────────────
check('kind labels are the ones the design asks for',
  P.kindLabel('dashboard') === 'report' && P.kindLabel('image') === 'image' &&
  P.kindLabel('pdf') === 'pdf');
check('an unknown kind falls back to itself', P.kindLabel('zip') === 'zip');

// ── PDF split (real pdf-lib — the vendored UMD works in node too) ────
const PDFLib = require(path.join(ROOT, 'static', 'vendor', 'pdf-lib.min.js'));
check('page name: «report.pdf» page 3 of 12',
  P.pdfPageName('report.pdf', 3, 12) === 'report — page 3 of 12.pdf');
check('page name: extension is case-insensitive',
  P.pdfPageName('DOC.PDF', 1, 2) === 'DOC — page 1 of 2.pdf');

(async () => {
  const src = await PDFLib.PDFDocument.create();
  src.addPage([200, 100]);
  src.addPage([300, 150]);
  src.addPage([400, 200]);
  const bytes = await src.save();
  const pages = await P.splitPdf(PDFLib, bytes);
  check('splitPdf: 3-page PDF → 3 files', pages.length === 3, String(pages.length));
  let allPdf = true, allOnePage = true, sizesKept = true;
  const widths = [200, 300, 400];
  for (let i = 0; i < pages.length; i++) {
    const head = Buffer.from(pages[i].slice(0, 5)).toString('latin1');
    if (head !== '%PDF-') allPdf = false;
    const doc = await PDFLib.PDFDocument.load(pages[i]);
    if (doc.getPageCount() !== 1) allOnePage = false;
    if (Math.round(doc.getPage(0).getWidth()) !== widths[i]) sizesKept = false;
  }
  check('splitPdf: every file starts with %PDF- (server magic-byte sniff passes)', allPdf);
  check('splitPdf: every file holds exactly one page', allOnePage);
  check('splitPdf: page order and dimensions preserved', sizesKept);

  console.log();
  if (FAILURES.length) {
    console.log(FAILURES.length + ' FAILED: ' + FAILURES.join(', '));
    process.exit(1);
  }
  console.log('ALL PASS');
})();
