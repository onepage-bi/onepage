/* Headless harness for static/js/quests-page.js and the templates/pages/
 * quests.html id contract that its DOM part (and phase 7's quests-how.js)
 * bind to.
 *
 * static/js/quests-page.js follows the pure-block-first idiom pinned by
 * tests/dashboards_dom.js: pure helpers are exported via `module.exports`
 * BEFORE the file touches `document`, guarded by
 * `if (typeof document === 'undefined') return;` — requiring the module
 * with no `document` global runs only that pure block.
 *
 * Run: node tests/quests_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

const Q = require(path.join(ROOT, 'static', 'js', 'quests-page.js'));
check('module exports the pure helpers without a DOM',
  Q && typeof Q.pluralForm === 'function' && typeof Q.levelLabel === 'function' &&
  typeof Q.renderPathModel === 'function' && typeof Q.chanceRows === 'function');

// ── pluralForm ───────────────────────────────────────────────────────
(function () {
  check('pluralForm(1) -> one', Q.pluralForm(1) === 0);
  check('pluralForm(21) -> one (mod100 != 11)', Q.pluralForm(21) === 0);
  check('pluralForm(11) -> many (the 11-14 exception)', Q.pluralForm(11) === 2);
  check('pluralForm(2) -> few', Q.pluralForm(2) === 1);
  check('pluralForm(4) -> few', Q.pluralForm(4) === 1);
  check('pluralForm(22) -> few', Q.pluralForm(22) === 1);
  check('pluralForm(0) -> many', Q.pluralForm(0) === 2);
  check('pluralForm(5) -> many', Q.pluralForm(5) === 2);
  check('pluralForm(12) -> many', Q.pluralForm(12) === 2);
})();

// ── levelLabel — mirrors app/services/quests.py LEVELS ──────────────
(function () {
  check('levelLabel(0) -> Novice', Q.levelLabel(0) === 'Novice');
  check('levelLabel(7) -> Novice (below first threshold)', Q.levelLabel(7) === 'Novice');
  check('levelLabel(8) -> Practitioner (exact threshold)', Q.levelLabel(8) === 'Practitioner');
  check('levelLabel(17) -> Practitioner', Q.levelLabel(17) === 'Practitioner');
  check('levelLabel(18) -> Confident user', Q.levelLabel(18) === 'Confident user');
  check('levelLabel(29) -> Confident user', Q.levelLabel(29) === 'Confident user');
  check('levelLabel(30) -> Expert', Q.levelLabel(30) === 'Expert');
  check('levelLabel(999) -> Expert (no ceiling)', Q.levelLabel(999) === 'Expert');
  check('levelLabel(null) -> Novice (no throw)', Q.levelLabel(null) === 'Novice');
})();

// ── shortDate / fmtDay ───────────────────────────────────────────────
(function () {
  check('shortDate: YYYY-MM-DD -> DD.MM', Q.shortDate('2026-09-04') === '04.09', Q.shortDate('2026-09-04'));
  check('shortDate: empty -> empty', Q.shortDate('') === '');
  check('fmtDay(1, 15)', Q.fmtDay(1, 15) === 'Day 1 of 15', Q.fmtDay(1, 15));
  check('fmtDay defaults total to 15', Q.fmtDay(3) === 'Day 3 of 15');
})();

// ── renderPathModel — 7 entries, today flag, milestone tags ─────────
(function () {
  const state = {
    path: [
      { day: '2026-09-04', offset: 0, streak_n: 3, rare_x2: true, extra: false, today: true, done: true },
      { day: '2026-09-07', offset: 1, streak_n: 4, rare_x2: false, extra: false, today: false, done: false },
      { day: '2026-09-08', offset: 2, streak_n: 5, rare_x2: false, extra: false, today: false, done: false },
      { day: '2026-09-09', offset: 3, streak_n: 6, rare_x2: true, extra: false, today: false, done: false },
      { day: '2026-09-10', offset: 4, streak_n: 7, rare_x2: false, extra: true, today: false, done: false },
      { day: '2026-09-11', offset: 5, streak_n: 8, rare_x2: false, extra: false, today: false, done: false },
      { day: '2026-09-14', offset: 6, streak_n: 9, rare_x2: true, extra: false, today: false, done: false },
    ],
  };
  const model = Q.renderPathModel(state);
  check('renderPathModel: 7 entries', model.length === 7, model.length);
  check('renderPathModel: today entry flagged and done', model[0].today === true && model[0].done === true);
  check('renderPathModel: today entry has no future flag', model[0].future === false);
  check('renderPathModel: future entries flagged', model[1].future === true && model[1].today === false);
  check('renderPathModel: today label is "Today"', model[0].dayLabel === 'Today');
  check('renderPathModel: future label is short date', model[1].dayLabel === '07.09', model[1].dayLabel);
  check('renderPathModel: extra-surprise day tags "2 surprises"',
    model[4].tag && model[4].tag.text === '2 surprises' && model[4].tag.variant === 'two', model[4].tag);
  check('renderPathModel: rare_x2-only day tags "×2 rare"',
    model[3].tag && model[3].tag.text === '×2 rare' && model[3].tag.variant === null, model[3].tag);
  check('renderPathModel: a done day carries no tag (even if milestone)', model[0].tag === null, model[0].tag);
  check('renderPathModel: a plain day carries no tag', model[1].tag === null);

  // nextStep: what "Next surprise …" names — entry 1 on a working day,
  // entry 0 on a weekend (no step is flagged today)
  check('nextStep: working day → entry 1', Q.nextStep(state.path).day === '2026-09-07');
  const wk = state.path.map(function (p) { return Object.assign({}, p, { today: false, done: false }); });
  check('nextStep: weekend → entry 0', Q.nextStep(wk).day === '2026-09-04');
  check('nextStep: empty path → null', Q.nextStep([]) === null && Q.nextStep(undefined) === null);
  const wkModel = Q.renderPathModel({ path: wk });
  check('renderPathModel: weekend — no "Today" label', wkModel.every(function (m) { return m.dayLabel !== 'Today'; }));
  check('renderPathModel: empty state -> empty array (no throw)', Q.renderPathModel({}).length === 0);
})();

// ── chanceRows — sums to ~100%, names resolved from card_meta ───────
(function () {
  const state = {
    chances: [
      { card_id: 'watch', pct: 20 },
      { card_id: 'chat', pct: 18 },
      { card_id: 'star', pct: 2 },
    ],
    card_meta: {
      watch: { name: 'Watcher Fox', rarity: 'common' },
      chat: { name: 'Chatty Fox', rarity: 'common' },
      star: { name: 'Stargazer Fox', rarity: 'legendary' },
    },
  };
  const rows = Q.chanceRows(state);
  check('chanceRows: one row per chance', rows.length === 3, rows.length);
  check('chanceRows: names resolved from card_meta',
    rows[0].name === 'Watcher Fox' && rows[2].name === 'Stargazer Fox');
  const sum = rows.reduce(function (a, r) { return a + r.pct; }, 0);
  check('chanceRows: pct sums back to the input total', Math.abs(sum - 40) < 1e-9, sum);
  check('chanceRows: unknown card falls back to its id',
    Q.chanceRows({ chances: [{ card_id: 'ghost', pct: 5 }], card_meta: {} })[0].name === 'ghost');
  check('chanceRows: empty state -> empty array (no throw)', Q.chanceRows({}).length === 0);
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

  const requiredIds = [
    'q-root', 'q-hero', 'q-cap', 'q-open', 'q-path', 'q-quests',
    'q-quiz-banner', 'q-rating', 'q-collection',
    // show-how layers (phase 7, quests-how.js)
    'q-dimT', 'q-dimB', 'q-dimL', 'q-dimR', 'q-spot', 'q-ghost', 'q-callout',
    'q-keys', 'q-mini', 'q-howbar', 'q-how-again', 'q-how-ok', 'q-how-x',
  ];
  requiredIds.forEach(function (id) {
    const re = new RegExp('id="' + id + '"');
    check('quests.html has id="' + id + '"', re.test(tpl));
  });

  check('quests.html does not use the `| safe` filter anywhere', !/\|\s*safe/.test(tpl));

  check('q-root carries data-state', /id="q-root"[^>]*data-state="\{\{ state_json/.test(tpl) ||
    /data-state="\{\{ state_json/.test(tpl));

  check('quests.html extends base.html', /\{% extends "base\.html" %\}/.test(tpl));
  check('quests.html includes the sidebar component', /include "components\/sidebar\.html"/.test(tpl));
  check('quests.html includes the topbar component', /include "components\/topbar\.html"/.test(tpl));
  check('quests.html includes the palette component', /include "components\/palette\.html"/.test(tpl));

  ['quests-fonts.css', 'quests.css', 'quests-page.js', 'quests-how.js', 'palette.js', 'hotkeys.js'].forEach(function (asset) {
    check('quests.html references ' + asset, tpl.indexOf(asset) !== -1);
  });

  check('every static asset link carries the ?v={{ asset_v }} cache-buster',
    !/(quests\.css|quests-fonts\.css|quests-page\.js|quests-how\.js)"(?!\?)/.test(tpl));
})();

console.log(FAILURES.length ? '\n' + FAILURES.length + ' FAILURES' : '\nALL PASS');
process.exit(FAILURES.length ? 1 : 0);
