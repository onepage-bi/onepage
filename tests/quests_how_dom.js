/* Headless harness for static/js/quests-how.js — the "Show how" guided
 * overlay engine on /quests.
 *
 * Unlike the usual pure-block-first idiom (tests/dashboards_dom.js,
 * tests/quests_dom.js), quests-how.js keeps `howEnd`/`_timers` in its pure
 * block too (every DOM touch inside is guarded and no-ops without
 * `document`) — see the file's own header comment — so requiring it with NO
 * `document` global still yields a fully usable module for every check
 * below, including the timer-cleanup one.
 *
 * Run: node tests/quests_how_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

const H = require(path.join(ROOT, 'static', 'js', 'quests-how.js'));
check('module exports HOW/dimRects/calloutPos/validateScenario without a DOM',
  H && typeof H.HOW === 'object' && typeof H.dimRects === 'function' &&
  typeof H.calloutPos === 'function' && typeof H.validateScenario === 'function');
check('module also exports howEnd/_timers/showHow (pure-guarded, no DOM needed)',
  typeof H.howEnd === 'function' && Array.isArray(H._timers) && typeof H.showHow === 'function');

// ── HOW scenario table ──────────────────────────────────────────────
(function () {
  const expectedKeys = ['theme', 'fav', 'grid', 'share', 'pres'];
  const keys = Object.keys(H.HOW).sort();
  check('HOW has exactly the 5 quest scenarios (pres/fav/theme/grid/share from app/services/quests.py)',
    JSON.stringify(keys) === JSON.stringify(expectedKeys.slice().sort()), keys.join(','));

  // Allow-list: every `sel` that targets the LIVE page (not the mock
  // #q-mini screen) must be one of these three real selectors.
  const ALLOWED_LIVE_SELECTORS = new Set([
    '.nav-item[href="/"]',
    '.nav-item[href="/presentations"]',
    '.palette-row[data-action="toggle:theme"]',
  ]);

  Object.keys(H.HOW).forEach(function (key) {
    const steps = H.HOW[key];
    check(key + ': validateScenario() returns no problems',
      H.validateScenario(steps).length === 0, JSON.stringify(H.validateScenario(steps)));
    check(key + ': at least one step (scenario is non-empty)', steps.length > 0);

    steps.forEach(function (s, i) {
      if (typeof s.sel !== 'string') return;
      const isMiniScoped = s.sel.indexOf('#q-mini') === 0;
      const isAllowedLive = ALLOWED_LIVE_SELECTORS.has(s.sel);
      check(key + '[' + i + ']: sel "' + s.sel + '" is an allowed live selector or scoped to #q-mini',
        isMiniScoped || isAllowedLive);
    });

    // Every step with sel/mini/keys/fx/fxTheme must carry a label (also
    // covered by validateScenario, checked again explicitly per the spec).
    steps.forEach(function (s, i) {
      const needsLabel = !!(s.sel || s.mini || s.keys || s.fx || s.fxTheme);
      if (needsLabel) check(key + '[' + i + ']: has a label', typeof s.label === 'string' && s.label.length > 0);
      check(key + '[' + i + ']: ms is a positive number', typeof s.ms === 'number' && s.ms > 0);
    });
  });

  check('validateScenario: empty array -> one problem', H.validateScenario([]).length === 1);
  check('validateScenario: not an array -> one problem', H.validateScenario(null).length === 1);
  check('validateScenario: missing label on a sel step -> problem',
    H.validateScenario([{ sel: '.x', ms: 100 }]).length > 0);
  check('validateScenario: ms <= 0 -> problem',
    H.validateScenario([{ label: 'l', mini: 'm', ms: 0 }]).length > 0);
  check('validateScenario: non-string sel -> problem',
    H.validateScenario([{ label: 'l', sel: 123, ms: 100 }]).length > 0);
  check('validateScenario: keys with wrong length -> problem',
    H.validateScenario([{ label: 'l', keys: ['Ctrl'], ms: 100 }]).length > 0);
  check('validateScenario: a well-formed step-only-ms is fine',
    H.validateScenario([{ pulse: true, ms: 500 }]).length === 0);
})();

// ── dimRects — four panels tile the viewport around the hole ────────
(function () {
  const W = 1280, H_ = 800;
  const rect = { top: 200, bottom: 260, left: 300, right: 500 };
  const r = H.dimRects(rect, W, H_);
  check('dimRects returns top/bottom/left/right', !!(r.top && r.bottom && r.left && r.right));

  const area = function (p) { return Math.max(0, p.width) * Math.max(0, p.height); };
  const totalArea = area(r.top) + area(r.bottom) + area(r.left) + area(r.right);
  const holeArea = (rect.right - rect.left) * (rect.bottom - rect.top);
  const expected = W * H_ - holeArea;
  check('dimRects: the 4 panels cover exactly the viewport minus the hole',
    Math.abs(totalArea - expected) < 1e-6, totalArea + ' vs ' + expected);

  check('dimRects: top panel spans full width, from 0 to rect.top',
    r.top.left === 0 && r.top.top === 0 && r.top.width === W && r.top.height === rect.top);
  check('dimRects: bottom panel starts at rect.bottom and reaches viewport bottom',
    r.bottom.top === rect.bottom && r.bottom.height === H_ - rect.bottom);
  check('dimRects: left/right panels span exactly the hole\'s vertical band',
    r.left.height === rect.bottom - rect.top && r.right.height === rect.bottom - rect.top);

  // Degenerate: a rect outside the viewport still yields non-negative sizes.
  const rNeg = H.dimRects({ top: -50, bottom: -10, left: -20, right: -5 }, W, H_);
  check('dimRects: out-of-viewport rect never yields negative width/height',
    rNeg.top.height >= 0 && rNeg.bottom.height >= 0 && rNeg.left.width >= 0 && rNeg.right.width >= 0);
})();

// ── calloutPos — always inside the viewport ──────────────────────────
(function () {
  const W = 1280, H_ = 800;
  const inside = function (pos) {
    return pos.left >= 0 && pos.top >= 0 && pos.left <= W && pos.top <= H_;
  };

  const p1 = H.calloutPos({ left: 100, top: 100, width: 40, height: 20 }, null, W, H_);
  check('calloutPos: real-target case stays inside the viewport', inside(p1), JSON.stringify(p1));

  const p2 = H.calloutPos(null, { left: 900, top: 700, width: 440, height: 90, bottom: 790 }, W, H_);
  check('calloutPos: mini-screen case stays inside the viewport', inside(p2), JSON.stringify(p2));

  const p3 = H.calloutPos(null, null, W, H_);
  check('calloutPos: no-target (keys) case centers and stays inside the viewport', inside(p3) && p3.center === true, JSON.stringify(p3));

  // Near the far edge — must clamp, not overflow.
  const p4 = H.calloutPos({ left: W - 5, top: H_ - 5, width: 10, height: 10 }, null, W, H_);
  check('calloutPos: near-edge target still clamps inside the viewport', inside(p4), JSON.stringify(p4));

  const p5 = H.calloutPos(null, { left: -50, top: -50, width: 100, height: 100, bottom: 50 }, W, H_);
  check('calloutPos: mini-screen rect off the top-left still clamps to >= 0', p5.left >= 0 && p5.top >= 0, JSON.stringify(p5));
})();

// ── howEnd() timer cleanup — fake clearTimeout counter, no DOM needed ─
(function () {
  const seenClears = [];
  const realClearTimeout = global.clearTimeout;
  global.clearTimeout = function (id) { seenClears.push(id); };

  H._timers.length = 0;
  H._timers.push('fake-id-1', 'fake-id-2', 'fake-id-3');
  H.howEnd();

  check('howEnd(): calls clearTimeout once per pending timer',
    seenClears.length === 3 && seenClears.join(',') === 'fake-id-1,fake-id-2,fake-id-3', seenClears.join(','));
  check('howEnd(): empties _timers afterwards (same array identity)', H._timers.length === 0);
  check('howEnd(): does not throw when called again on an already-empty queue', (function () {
    try { H.howEnd(); return true; } catch (e) { return false; }
  })());

  global.clearTimeout = realClearTimeout;
})();

// ── Template contract: templates/pages/quests.html ──────────────────
(function () {
  const tplPath = path.join(ROOT, 'templates', 'pages', 'quests.html');
  let tpl = '';
  try {
    tpl = fs.readFileSync(tplPath, 'utf8');
  } catch (e) {
    check('templates/pages/quests.html exists', false, e.message);
    return;
  }
  check('templates/pages/quests.html exists', true);

  const layerIds = [
    'q-dimT', 'q-dimB', 'q-dimL', 'q-dimR', 'q-spot', 'q-ghost', 'q-callout',
    'q-keys', 'q-mini', 'q-howbar', 'q-how-again', 'q-how-ok', 'q-how-x',
    // stable delegation ancestor + the row buttons quests-page.js renders
    'q-quests',
  ];
  layerIds.forEach(function (id) {
    check('quests.html has id="' + id + '"', new RegExp('id="' + id + '"').test(tpl));
  });

  check('quests.html references quests-how.js', tpl.indexOf('quests-how.js') !== -1);
})();

// ── CSS contract: the `[hidden]` override survives (main.css ships no
// global `[hidden]{display:none}`, and .q-keys/.q-mini/.q-howbar/.q-how-x
// set an explicit `display`, which otherwise beats the UA default) ──────
(function () {
  const cssPath = path.join(ROOT, 'static', 'css', 'quests.css');
  let css = '';
  try {
    css = fs.readFileSync(cssPath, 'utf8');
  } catch (e) {
    check('static/css/quests.css exists', false, e.message);
    return;
  }
  check('static/css/quests.css exists', true);

  const hiddenSelectors = [
    '.q-dim[hidden]', '#q-spot[hidden]', '#q-ghost[hidden]', '#q-callout[hidden]',
    '#q-keys[hidden]', '#q-mini[hidden]', '#q-howbar[hidden]', '#q-how-x[hidden]',
  ];
  hiddenSelectors.forEach(function (sel) {
    const escaped = sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    check('quests.css has a display:none rule for ' + sel, new RegExp(escaped + '[^}]*display:\\s*none').test(css));
  });

  // Every id that has both a "sets display" rule AND a plain-selector
  // (non-[hidden]) appearance must also appear in a `[hidden]` rule —
  // guards against a future layer id being added to the CSS block above
  // with a display property but no matching [hidden] override.
  ['.q-keys', '.q-howbar', '.q-how-x'].forEach(function (cls) {
    const setsDisplay = new RegExp('\\' + cls + '\\s*\\{[^}]*display:', ).test(css) ||
      new RegExp('#' + cls.slice(1) + '\\s*\\{[^}]*display:').test(css);
    check(cls + ' sets an explicit display (sanity check for the rule above)', setsDisplay);
  });
})();

console.log(FAILURES.length ? '\n' + FAILURES.length + ' FAILURES' : '\nALL PASS');
process.exit(FAILURES.length ? 1 : 0);
