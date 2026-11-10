/* Headless harness for the "Chat about the report" pane in static/js/viewer.js.
 *
 * Same idiom as tests/presentations_dom.js: viewer.js keeps its chat helpers
 * and `mountChatPane` above a `typeof document === 'undefined'` bail-out, so
 * node can require the real file and mount the real pane over the smallest
 * fake document it touches — no jsdom, no new dependency.
 *
 * What is pinned here is what P1 promised the owner:
 *   1. the pane PUSHES the report — it is a grid column, so the width lands
 *      on --chat-w of #viewer-root and the root carries data-chat;
 *   2. the width is clamped 340…62vw, "wider" is 50vw, a double-click on the
 *      grip resets to 440;
 *   3. the state {open, w, wide} survives a reload, and a browser holding the
 *      OLD one-string format ('open'/'closed') keeps its pane;
 *   4. Escape closes the pane — but never while someone is typing, never
 *      while the focus is inside the chat frame, never in present mode and
 *      never after another handler already took the key;
 *   5. the hot path: a remembered open pane creates NO iframe until the
 *      report has painted (op_ready / idle).
 *
 * The fake DOM has no descendant selectors on purpose: every node the pane
 * touches is addressed by id.
 *
 * Run: node tests/viewer_chat_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

const V = require(path.join(ROOT, 'static', 'js', 'viewer.js'));
check('viewer.js exports its chat helpers without a DOM',
  V && typeof V.mountChatPane === 'function' && typeof V.chatClampW === 'function');

// ═══════════════════════════════════════════════════════════════════
// Part 1 — the pure half
// ═══════════════════════════════════════════════════════════════════
const VW = 1600;   // 62% = 992, 50% = 800

check('width: the default fits', V.chatClampW(440, VW) === 440, String(V.chatClampW(440, VW)));
check('width: never narrower than 340', V.chatClampW(100, VW) === 340, String(V.chatClampW(100, VW)));
check('width: never wider than 62vw', V.chatClampW(1400, VW) === 992, String(V.chatClampW(1400, VW)));
check('width: garbage falls back to the default', V.chatClampW('nope', VW) === 440,
  String(V.chatClampW('nope', VW)));
check('width: on a narrow window the floor wins over 62vw (the overlay case)',
  V.chatClampW(440, 400) === 340, String(V.chatClampW(440, 400)));
// The ceiling is arithmetic, not CSS: the report keeps 560px and the dock
// takes its own width off the top — that is the 2026-08-23 squeeze, prevented.
check('width: the report keeps 560px — the chat gets what is left',
  V.chatMaxW(1200, 0) === 640, String(V.chatMaxW(1200, 0)));
check('width: an open dock is the chat\'s rival for the same width',
  V.chatMaxW(1360, 360) === 440, String(V.chatMaxW(1360, 360)));
check('width: closing the dock really gives the chat more room',
  V.chatMaxW(1360, 0) === 800, String(V.chatMaxW(1360, 0)));
check('width: 62vw still caps a very wide window',
  V.chatMaxW(3000, 0) === 1860, String(V.chatMaxW(3000, 0)));
check('width: when nothing fits, the chat keeps its own 340 floor and the '
  + 'report is the one that yields',
  V.chatMaxW(700, 360) === 340, String(V.chatMaxW(700, 360)));
check('width: a dock the pane must share with is respected by the clamp',
  V.chatClampW(900, 1360, 360) === 440, String(V.chatClampW(900, 1360, 360)));
check('width: "wider" is half the window', V.chatWideW(VW) === 800, String(V.chatWideW(VW)));
check('width: a closed pane is a zero column',
  V.chatPaneW({ open: false, w: 440, wide: false }, VW) === 0);
check('width: wide beats the stored width',
  V.chatPaneW({ open: true, w: 400, wide: true }, VW) === 800);

// ── persisted state ───────────────────────────────────────────────────
check('state: nothing stored -> closed, 440, not wide',
  JSON.stringify(V.chatReadState(null)) === '{"open":false,"w":440,"wide":false}',
  JSON.stringify(V.chatReadState(null)));
check('state: the OLD one-string format keeps an open pane',
  V.chatReadState('open').open === true && V.chatReadState('only').open === true
  && V.chatReadState('closed').open === false);
check('state: round-trips through the JSON format',
  JSON.stringify(V.chatReadState(V.chatWriteState({ open: true, w: 512, wide: true })))
  === '{"open":true,"w":512,"wide":true}',
  V.chatWriteState({ open: true, w: 512, wide: true }));
check('state: corrupt JSON is not an open pane', V.chatReadState('{oops').open === false);

// ── who owns Escape ───────────────────────────────────────────────────
const ESC = { key: 'Escape', defaultPrevented: false };
check('esc: closes an open pane', V.chatEscCloses(ESC, { open: true }) === true);
check('esc: does nothing when the pane is closed', V.chatEscCloses(ESC, { open: false }) === false);
check('esc: a handler that already took the key wins (presentations.js)',
  V.chatEscCloses({ key: 'Escape', defaultPrevented: true }, { open: true }) === false);
check('esc: present mode keeps Esc — the pane stays visible mid-show',
  V.chatEscCloses(ESC, { open: true, present: true }) === false);
check('esc: fullscreen keeps Esc',
  V.chatEscCloses(ESC, { open: true, fullscreen: true }) === false);
check('esc: typing in a field never closes the pane',
  V.chatEscCloses(ESC, { open: true, active: { tagName: 'TEXTAREA' } }) === false);
check('esc: contenteditable counts as typing',
  V.chatEscCloses(ESC, { open: true, active: { tagName: 'DIV', isContentEditable: true } }) === false);
check('esc: focus INSIDE the chat frame belongs to the frame (it posts op_chat_close)',
  V.chatEscCloses(ESC, { open: true, active: { tagName: 'IFRAME' } }) === false);
check('esc: another key is not Escape', V.chatEscCloses({ key: 'a' }, { open: true }) === false);
check('wide key: ⌘/Ctrl+Shift+]',
  V.chatWideKey({ metaKey: true, shiftKey: true, key: ']' }) === true
  && V.chatWideKey({ ctrlKey: true, shiftKey: true, key: ']' }) === true);
check('wide key: without Shift it is not the shortcut',
  V.chatWideKey({ metaKey: true, key: ']' }) === false);
check('wide key: a plain ] is the dock toggle, not the chat',
  V.chatWideKey({ key: ']' }) === false);

// ═══════════════════════════════════════════════════════════════════
// Part 2 — the pane, mounted over a fake document
// ═══════════════════════════════════════════════════════════════════
class ClassList {
  constructor() { this.set = new Set(); }
  add() { for (const c of arguments) if (c) this.set.add(c); }
  remove() { for (const c of arguments) this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}
class El {
  constructor(tag, id) {
    this.tagName = String(tag).toUpperCase();
    this.id = id || '';
    this.children = []; this.parent = null; this.attrs = {}; this.listeners = {};
    this.hidden = false; this.focused = 0;
    this._class = new ClassList();
    this.props = {};
    this.style = { props: {}, setProperty: (k, v) => { this.style.props[k] = v; } };
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  appendChild(c) { c.parent = this; this.children.push(c); DOC.byId[c.id] = c; return c; }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this);
    if (DOC.byId[this.id] === this) delete DOC.byId[this.id];
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(type, ev) {
    const e = Object.assign({ type: type, preventDefault() { this.defaultPrevented = true; }, defaultPrevented: false }, ev || {});
    (this.listeners[type] || []).forEach(f => f.call(this, e));
    return e;
  }
  focus() { this.focused++; DOC.activeElement = this; }
  setPointerCapture() { this.captured = true; }
  releasePointerCapture() { this.captured = false; }
}

let DOC = null;
function fakeWorld(stored, opts) {
  const o = opts || {};
  const byId = {};
  ['viewer-root', 'discuss-pane', 'discuss-fab', 'discuss-count',
   'discuss-grip', 'discuss-wide', 'discuss-close', 'discuss-body'].forEach(id => {
    byId[id] = new El(id === 'discuss-pane' ? 'aside' : 'div', id);
  });
  byId['discuss-body'].parent = byId['discuss-pane'];
  // The pane's own fallback header: hidden once the embedded page (which
  // carries its own title/close/wide) has spoken.
  // The attribute alone is not enough: main.css gives the header `display: flex`,
// which outranks the UA's [hidden] rule, so the stylesheet must carry the
// explicit override — otherwise the pane shows two headers (stand 2026-09-07).
const mainCss = require('fs').readFileSync(require('path').join(__dirname, '..', 'static', 'css', 'main.css'), 'utf8');
check('main.css hides .v-chat-head[hidden]', /\.v-chat-head\[hidden\]\s*\{[^}]*display:\s*none/.test(mainCss));
const head = new El('header', 'discuss-head'); head.attrs['class'] = 'v-chat-head';
  head.parent = byId['discuss-pane']; byId['discuss-pane'].children.push(head); byId['discuss-head'] = head;
  byId['discuss-pane'].querySelector = (sel) => (sel === '.v-chat-head' ? head : null);
  const doc = {
    byId: byId,
    body: new El('body'),
    activeElement: null,
    fullscreenElement: null,
    listeners: {},
    getElementById(id) { return byId[id] || null; },
    createElement(tag) { return new El(tag); },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    fire(type, ev) {
      const e = Object.assign({ type: type, preventDefault() { this.defaultPrevented = true; }, defaultPrevented: false }, ev || {});
      (this.listeners[type] || []).forEach(f => f(e));
      return e;
    },
  };
  DOC = doc;
  const mem = new Map();
  if (stored !== undefined && stored !== null) mem.set('op-viewer-chat:d7', stored);
  const timers = [];
  const win = {
    innerWidth: o.vw || VW,
    location: { origin: 'https://op.local' },
    listeners: {},
    idleCalls: [],
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    fire(type, ev) {
      const e = Object.assign({ type: type }, ev || {});
      (this.listeners[type] || []).forEach(f => f(e));
      return e;
    },
    setTimeout(fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; },
    clearTimeout() {},
    timers: timers,
  };
  if (o.idle !== false) win.requestIdleCallback = function (fn) { win.idleCalls.push(fn); };
  const storage = {
    mem: mem,
    getItem(k) { return mem.has(k) ? mem.get(k) : null; },
    setItem(k, v) { mem.set(k, String(v)); },
  };
  const api = V.mountChatPane({ doc: doc, win: win, storage: storage, reportId: 'd7' });
  return { doc: doc, win: win, storage: storage, api: api, el: byId };
}

// ── 1. closed by default: no frame, no column ─────────────────────────
{
  const w = fakeWorld(null);
  check('mount: a report nobody has chatted about opens closed',
    w.el['viewer-root'].getAttribute('data-chat') === 'closed' && w.el['discuss-pane'].hidden === true);
  check('mount: no iframe exists before the pane is opened', w.api.hasFrame() === false);
  check('mount: the closed column is 0px',
    w.el['viewer-root'].style.props['--chat-w'] === '0px', w.el['viewer-root'].style.props['--chat-w']);

  w.el['discuss-fab'].fire('click');
  check('open: the FAB opens the pane',
    w.el['viewer-root'].getAttribute('data-chat') === 'open' && w.el['discuss-pane'].hidden === false);
  check('open: the report is pushed by exactly --chat-w',
    w.el['viewer-root'].style.props['--chat-w'] === '440px', w.el['viewer-root'].style.props['--chat-w']);
  check('open: the frame is built now, and inside #discuss-body',
    w.api.hasFrame() === true && w.el['discuss-body'].children.length === 1);
  const frame = w.el['discuss-body'].children[0];
  check('open: the frame is /explore in chat mode for THIS report',
    frame.src === '/explore?report=d7&mode=chat&embed=1', frame.src);
  check('open: the state is persisted',
    w.storage.getItem('op-viewer-chat:d7') === '{"open":true,"w":440,"wide":false}',
    w.storage.getItem('op-viewer-chat:d7'));

  // The frame is same-origin, so the width is posted into it — the composer
  // placeholder shortens on a narrow pane.
  const posts = [];
  frame.contentWindow = { postMessage: (m, origin) => posts.push({ m: m, origin: origin }) };
  w.api.setWide(true);
  check('wide: "wider" is 50vw and lands on the column',
    w.el['viewer-root'].style.props['--chat-w'] === '800px', w.el['viewer-root'].style.props['--chat-w']);
  check('wide: the frame is told the new width',
    posts.length === 1 && posts[0].m.type === 'op_chat_width'
    && posts[0].m.w === 800 && posts[0].m.wide === true, JSON.stringify(posts[0] && posts[0].m));
  check('wide: the width is posted to our own origin only',
    posts[0].origin === 'https://op.local', posts[0].origin);
  check('wide: the button reports its state',
    w.el['discuss-wide'].getAttribute('aria-pressed') === 'true');
  w.el['discuss-wide'].fire('click');
  check('wide: the button toggles back',
    w.api.state().wide === false && w.el['viewer-root'].style.props['--chat-w'] === '440px');

  // ── the grip ────────────────────────────────────────────────────────
  const grip = w.el['discuss-grip'];
  grip.fire('pointerdown', { clientX: 1000, pointerId: 1 });
  check('grip: dragging suspends the transition',
    w.el['viewer-root'].classList.contains('is-chat-resizing') === true);
  grip.fire('pointermove', { clientX: 900 });   // dragged 100px to the LEFT -> wider
  check('grip: dragging left widens the pane',
    w.api.state().w === 540, String(w.api.state().w));
  grip.fire('pointermove', { clientX: 100 });   // way past the 62vw ceiling
  check('grip: the drag is clamped at 62vw', w.api.state().w === 992, String(w.api.state().w));
  grip.fire('pointermove', { clientX: 1600 });  // way past the floor
  check('grip: the drag is clamped at 340', w.api.state().w === 340, String(w.api.state().w));
  grip.fire('pointerup', { pointerId: 1 });
  check('grip: releasing restores the transition and persists',
    w.el['viewer-root'].classList.contains('is-chat-resizing') === false
    && JSON.parse(w.storage.getItem('op-viewer-chat:d7')).w === 340,
    w.storage.getItem('op-viewer-chat:d7'));
  grip.fire('pointermove', { clientX: 200 });
  check('grip: a move with no drag in progress changes nothing', w.api.state().w === 340);
  grip.fire('dblclick');
  check('grip: a double-click resets to 440',
    w.api.state().w === 440 && w.api.state().wide === false, String(w.api.state().w));

  // ── closing ─────────────────────────────────────────────────────────
  w.el['discuss-close'].fire('click');
  check('close: the column collapses and the pane hides',
    w.el['viewer-root'].getAttribute('data-chat') === 'closed'
    && w.el['discuss-pane'].hidden === true
    && w.el['viewer-root'].style.props['--chat-w'] === '0px');
  check('close: the focus goes back to the FAB', w.el['discuss-fab'].focused === 1);
  check('close: closing KEEPS the frame (the built index survives)', w.api.hasFrame() === true);
  check('close: the frame is dropped only after ten quiet minutes',
    w.win.timers[w.win.timers.length - 1].ms === 600000, String(w.win.timers[w.win.timers.length - 1].ms));
  w.win.timers[w.win.timers.length - 1].fn();
  check('close: … and then it really is gone', w.api.hasFrame() === false);
  check('close: the closed state is persisted',
    JSON.parse(w.storage.getItem('op-viewer-chat:d7')).open === false);
}

// ── 2. Escape, on a real mount ────────────────────────────────────────
{
  const w = fakeWorld('{"open":true,"w":460,"wide":false}');
  w.win.idleCalls.forEach(f => f());
  check('esc: the pane is open to begin with', w.api.state().open === true);
  const typing = new El('textarea');
  w.doc.activeElement = typing;
  w.doc.fire('keydown', { key: 'Escape' });
  check('esc: typing in a field does NOT close the pane', w.api.state().open === true);
  w.doc.activeElement = null;
  w.doc.body.classList.add('op-present');
  w.doc.fire('keydown', { key: 'Escape' });
  check('esc: in present mode the pane stays (Esc leaves the show)', w.api.state().open === true);
  w.doc.body.classList.remove('op-present');
  const ev = w.doc.fire('keydown', { key: 'Escape' });
  check('esc: otherwise it closes the pane', w.api.state().open === false);
  check('esc: and takes the key, so nothing else acts on it', ev.defaultPrevented === true);
  check('esc: the focus lands on the FAB', w.el['discuss-fab'].focused === 1);

  // ⌘⇧] from the page.
  w.el['discuss-fab'].fire('click');
  w.doc.fire('keydown', { key: ']', metaKey: true, shiftKey: true });
  check('keys: ⌘⇧] toggles the wide mode', w.api.state().wide === true);
}

// ── 3. the frame boundary ─────────────────────────────────────────────
{
  const w = fakeWorld(null);
  w.el['discuss-fab'].fire('click');
  const good = { origin: 'https://op.local' };
  w.win.fire('message', Object.assign({ data: { type: 'op_chat_count', n: 3 } }, good));
  check('count: a number from the frame lands on the badge as text',
    w.el['discuss-count'].textContent === '3' && w.el['discuss-count'].hidden === false,
    String(w.el['discuss-count'].textContent));
  check('head: once the frame has spoken, the pane\'s own header steps aside (no second "Close")',
    w.el['discuss-head'].hidden === true);
  w.win.fire('message', Object.assign({ data: { type: 'op_chat_count', n: '<img>' } }, good));
  check('count: anything that is not a number is 0, and 0 hides the badge',
    w.el['discuss-count'].textContent === '0' && w.el['discuss-count'].hidden === true);
  w.win.fire('message', { origin: 'https://evil.example', data: { type: 'op_chat_close' } });
  check('frame: a message from another origin is ignored', w.api.state().open === true);
  w.win.fire('message', Object.assign({ data: { type: 'op_chat_wide' } }, good));
  check('frame: op_chat_wide widens the pane', w.api.state().wide === true);
  w.win.fire('message', Object.assign({ data: { type: 'op_chat_close' } }, good));
  check('frame: op_chat_close closes the pane (Esc pressed inside the frame)',
    w.api.state().open === false);
  check('frame: … and the focus comes back out to the FAB', w.el['discuss-fab'].focused === 1);
}

// ── 4. the hot path: a remembered open pane costs no request ──────────
{
  const w = fakeWorld('{"open":true,"w":520,"wide":false}');
  check('hot path: the column is open on the very first paint',
    w.el['viewer-root'].getAttribute('data-chat') === 'open'
    && w.el['viewer-root'].style.props['--chat-w'] === '520px',
    w.el['viewer-root'].style.props['--chat-w']);
  check('hot path: but NO /explore frame exists yet — the report loads alone',
    w.api.hasFrame() === false);
  w.api.onReportReady();
  check('hot path: the frame appears once the report has painted (op_ready)',
    w.api.hasFrame() === true);
  w.api.onReportReady();
  check('hot path: a second op_ready does not build a second frame',
    w.el['discuss-body'].children.length === 1, String(w.el['discuss-body'].children.length));
}
{
  // A report that never says op_ready still gets its chat, from an idle slot.
  const w = fakeWorld('open');   // the OLD format, too
  check('hot path: the old one-string state still opens the pane, still lazily',
    w.api.state().open === true && w.api.hasFrame() === false);
  check('hot path: an idle callback is queued', w.win.idleCalls.length === 1);
  w.win.idleCalls.forEach(f => f());
  check('hot path: the idle callback builds the frame', w.api.hasFrame() === true);
}
{
  // No requestIdleCallback (Safari before 17): a timer is the fallback.
  const w = fakeWorld('open', { idle: false });
  check('hot path: with no requestIdleCallback a timer stands in',
    w.api.hasFrame() === false && w.win.timers.length === 1);
  w.win.timers[0].fn();
  check('hot path: … and it builds the frame', w.api.hasFrame() === true);
}

// ── 5. a resize re-clamps a pane that no longer fits ──────────────────
{
  const w = fakeWorld('{"open":true,"w":900,"wide":false}');
  check('resize: a wide window keeps the stored width',
    w.el['viewer-root'].style.props['--chat-w'] === '900px', w.el['viewer-root'].style.props['--chat-w']);
  w.win.innerWidth = 1000;
  w.win.fire('resize');
  check('resize: a narrower window re-clamps the column to what is left over '
    + 'the report\'s 560px',
    w.el['viewer-root'].style.props['--chat-w'] === '440px', w.el['viewer-root'].style.props['--chat-w']);
  check('resize: the stored width is NOT overwritten by a transient resize',
    JSON.parse(w.storage.getItem('op-viewer-chat:d7')).w === 900,
    w.storage.getItem('op-viewer-chat:d7'));
}

// ── Navigator: where the platform lets a person go ──────────────────────
// A link from a hub page is opened by the SHELL, not by the page itself: a tab
// opened from inside the sandbox inherits it and gets no cookie — Power BI
// Report Server shows a white screen (owner's observation, 29.10). This pure
// function decides about every address, and these are its rules.
{
  const B = 'https://onepage.example.com/d/abc';
  const kind = (u, t) => V.pageLinkAction(u, t || '', B).kind;

  check('navigator: a foreign https opens in a new tab',
    kind('https://pbirs.example.com/reports/powerbi/x') === 'new');
  check('navigator: an own address opens in the current tab',
    kind('/d/other') === 'same');
  check('navigator: an own address with target=_blank still opens a new tab',
    kind('/d/other', '_blank') === 'new');
  check('navigator: javascript: never opens',
    kind('javascript:alert(1)') === 'refuse');
  check('navigator: data: never opens',
    kind('data:text/html,<b>x') === 'refuse');
  check('navigator: file: is handed over as a path, not a tab',
    kind('file:///C:/data') === 'copy');
  // The URL parser treats backslashes as separators, and without this branch
  // a UNC path would turn into https://server/share — a meaningless tab.
  check('navigator: UNC is recognised before parsing and handed over as a path',
    kind('\\\\server\\share') === 'copy');
  check('navigator: an empty link does nothing', kind('') === 'none');
  check('navigator: an in-page anchor stays on the page',
    kind('#section') === 'same');

  // Share page: an anonymous reader has no platform session, and "own
  // address in the same tab" would send them to /login. Everything opens in
  // a new tab; the refusals are the same.
  const PL = require(path.join(ROOT, 'static', 'js', 'page-links.js'));
  const sk = (u, t) => PL.pageLinkAction(u, t || '', B, { standalone: true }).kind;
  check('share: the rules are the same module as the viewer uses',
    V.pageLinkAction === PL.pageLinkAction);
  check('share: own address opens in a new tab instead of leading to /login',
    sk('/d/other') === 'new');
  check('share: foreign https — new tab', sk('https://pbirs.example.com/x') === 'new');
  check('share: javascript: is refused there too', sk('javascript:alert(1)') === 'refuse');
  check('share: UNC is handed over as a path', sk('\\\\server\\share') === 'copy');
}

console.log('');
if (FAILURES.length) {
  console.log('FAILURES (' + FAILURES.length + '):');
  FAILURES.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log('ALL PASS');
