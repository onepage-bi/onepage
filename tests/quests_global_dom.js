/* Headless harness for static/js/quests.js pure helpers + the cross-page
 * wiring in base.html / sidebar.html / icons.html / palette.js / hotkeys.js.
 *
 * Same idiom as tests/catalog_dom.js: require the script directly (its
 * module.exports guard fires before any DOM is touched — quests.js:44-47),
 * call the pure helpers, and grep the template/script sources for the
 * static contract Phase 8 promised.
 *
 * Run: node tests/quests_global_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

// ── module.exports path (no DOM needed) ─────────────────────────────
const QUESTS = require(path.join(ROOT, 'static', 'js', 'quests.js'));

check('quests.js: exports localDateStr', typeof QUESTS.localDateStr === 'function');
check('quests.js: exports badgeFresh', typeof QUESTS.badgeFresh === 'function');
check('quests.js: exports eventKindFor', typeof QUESTS.eventKindFor === 'function');
// ADR-17b: the page gate that keeps /d/{id} and friends at zero extra requests
check('quests.js: exports companionPage', typeof QUESTS.companionPage === 'function');
check('companionPage: dashboards tab', QUESTS.companionPage('/'));
check('companionPage: dashboards with trailing slash', QUESTS.companionPage('/'));
check('companionPage: every other workspace page refused',
  !QUESTS.companionPage('/catalog') && !QUESTS.companionPage('/presentations') &&
  !QUESTS.companionPage('/data-sources'));
check('companionPage: report page refused', !QUESTS.companionPage('/d/abc'));
check('companionPage: explore refused', !QUESTS.companionPage('/explore'));
check('companionPage: admin refused', !QUESTS.companionPage('/admin/quests'));
check('companionPage: share refused', !QUESTS.companionPage('/s/token'));
check('companionPage: presentation show refused', !QUESTS.companionPage('/presentations/x/show'));

// ── localDateStr ─────────────────────────────────────────────────────
(function testLocalDateStr() {
  const d = new Date(2026, 8, 3); // 2026-09-03 (month is 0-based)
  check('localDateStr: pads month/day', QUESTS.localDateStr(d) === '2026-09-03', QUESTS.localDateStr(d));
  const d2 = new Date(2026, 0, 9); // 2026-01-09
  check('localDateStr: single-digit month and day', QUESTS.localDateStr(d2) === '2026-01-09', QUESTS.localDateStr(d2));
})();

// ── badgeFresh ────────────────────────────────────────────────────────
(function testBadgeFresh() {
  const NOW = 1_800_000_000_000; // arbitrary fixed instant
  check('badgeFresh: null cache is not fresh', QUESTS.badgeFresh(null, '2026-09-03', NOW) === false);
  check('badgeFresh: same date + open=true + recent check is fresh',
    QUESTS.badgeFresh({ date: '2026-09-03', open: true, checkedAt: NOW - 1000 }, '2026-09-03', NOW) === true);
  check('badgeFresh: open=true without timestamp is stale (legacy cache)',
    QUESTS.badgeFresh({ date: '2026-09-03', open: true }, '2026-09-03', NOW) === false);
  check('badgeFresh: open=true ages out after the TTL (a pending reward may appear)',
    QUESTS.badgeFresh({ date: '2026-09-03', open: true, checkedAt: NOW - 46 * 60 * 1000 }, '2026-09-03', NOW) === false);
  check('badgeFresh: stale date is not fresh',
    QUESTS.badgeFresh({ date: '2026-09-02', open: true }, '2026-09-03', NOW) === false);

  // Negative cache: fresh within the 45-minute TTL, stale after.
  const checkedAt = NOW - 10 * 60 * 1000; // 10 min ago
  check('badgeFresh: same date + open=false within 45min TTL is fresh',
    QUESTS.badgeFresh({ date: '2026-09-03', open: false, checkedAt }, '2026-09-03', NOW) === true);
  const staleCheckedAt = NOW - 46 * 60 * 1000; // 46 min ago
  check('badgeFresh: same date + open=false past 45min TTL is NOT fresh',
    QUESTS.badgeFresh({ date: '2026-09-03', open: false, checkedAt: staleCheckedAt }, '2026-09-03', NOW) === false);
  check('badgeFresh: open=false with no checkedAt is NOT fresh',
    QUESTS.badgeFresh({ date: '2026-09-03', open: false }, '2026-09-03', NOW) === false);
})();

// ── eventKindFor (fake elements with .closest()) ────────────────────
function fakeTarget(matches) {
  // matches: set of selectors this element (or an ancestor) satisfies
  return {
    closest(sel) { return matches.has(sel) ? this : null; }
  };
}
(function testEventKindFor() {
  const themeTarget = fakeTarget(new Set(['[data-action="toggle:theme"]']));
  check('eventKindFor: palette toggle:theme row -> theme', QUESTS.eventKindFor(themeTarget) === 'theme');

  const viewTarget = fakeTarget(new Set(['[data-view-btn="grid"]']));
  check('eventKindFor: grid view button -> view', QUESTS.eventKindFor(viewTarget) === 'view');

  const otherTarget = fakeTarget(new Set());
  check('eventKindFor: unrelated target -> null', QUESTS.eventKindFor(otherTarget) === null);

  check('eventKindFor: null target -> null', QUESTS.eventKindFor(null) === null);
  check('eventKindFor: target without closest() -> null', QUESTS.eventKindFor({}) === null);
})();

// ── static contract: base.html ───────────────────────────────────────
(function testBaseHtml() {
  const html = fs.readFileSync(path.join(ROOT, 'templates', 'base.html'), 'utf8');
  check('base.html: sets window.OP_QUESTS_ON', /window\.OP_QUESTS_ON\s*=/.test(html));
  check('base.html: OP_QUESTS_ON reflects settings.quests_enabled',
    /window\.OP_QUESTS_ON\s*=\s*\{\{\s*\(settings is defined and settings\.quests_enabled\)\s*\|\s*tojson\s*\}\}/.test(html));
  check('base.html: quests.js include exists',
    /\/static\/js\/quests\.js\?v=\{\{\s*asset_v\s*\}\}/.test(html));
  const includeGuardRe = /\{%\s*if user and settings is defined and settings\.quests_enabled\s*%\}\s*<script src="\/static\/js\/quests\.js/;
  check('base.html: quests.js include is guarded by user + settings.quests_enabled', includeGuardRe.test(html));
})();

// ── static contract: sidebar.html ────────────────────────────────────
(function testSidebarHtml() {
  const html = fs.readFileSync(path.join(ROOT, 'templates', 'components', 'sidebar.html'), 'utf8');
  check('sidebar.html: has a Quests nav item', /Quests/.test(html));
  check('sidebar.html: Quests item uses #ic-gift', /#ic-gift/.test(html));
  check('sidebar.html: Quests item links to \/quests', /href="\/quests"/.test(html));
  // On /quests the hooks stay alive (a real Ctrl+K → «Toggle theme» there must
  // count) while the capsule/badge stay off — the page owns those.
  {
    const qsrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'quests.js'), 'utf8');
    check('quests.js: /quests is a flag (ON_QUESTS), not an early return',
      /var ON_QUESTS = location\.pathname === '\/quests'/.test(qsrc) && !/pathname === '\/quests'[^\n]*\) return;/.test(qsrc));
    check('quests.js: on /quests the award is handed to the page as op:quest-award',
      /if \(ON_QUESTS\) \{[\s\S]*?new CustomEvent\('op:quest-award'/.test(qsrc));
    check('quests.js: on /quests no badge request is made',
      /if \(ON_QUESTS\) \{[\s\S]*?\} else if \(document\.readyState === 'loading'\)/.test(qsrc));
    const psrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'quests-page.js'), 'utf8');
    check('quests-page.js: listens to op:quest-award and refreshes the state',
      /addEventListener\('op:quest-award'[\s\S]*?refreshState\(\)/.test(psrc));
  }
  check('sidebar.html: Quests nav item is guarded by settings.quests_enabled',
    /\{%\s*if settings is defined and settings\.quests_enabled\s*%\}\s*\n\s*<a class="nav-item[^"]*"[^>]*href="\/quests"/.test(html));
  check('sidebar.html: admin section links to \/admin\/quests', /href="\/admin\/quests"/.test(html));
})();

// ── static contract: icons.html ──────────────────────────────────────
(function testIconsHtml() {
  const html = fs.readFileSync(path.join(ROOT, 'templates', 'components', 'icons.html'), 'utf8');
  check('icons.html: defines ic-gift symbol', /<symbol id="ic-gift"/.test(html));
})();

// ── static contract: palette.js ──────────────────────────────────────
(function testPaletteJs() {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'palette.js'), 'utf8');
  // Labels go through t(); the Russian text is now the catalogue key.
  check('palette.js: has a Quests command', /label:\s*t\('Quests'\)/.test(src));
  check('palette.js: Quests command binds G K', /label:\s*t\('Quests'\)[^\n]{0,150}kbd:\s*'G K'/.test(src));
  check('palette.js: Quests command does not reuse G Q',
    !/label:\s*t\('Quests'\)[^\n]{0,150}kbd:\s*'G Q'/.test(src));
  check('palette.js: existing Data Quality command still owns G Q',
    /label:\s*'Data Quality'[^\n]{0,150}kbd:\s*'G Q'/.test(src));
  check('palette.js: filters the Quests command by window.OP_QUESTS_ON', /OP_QUESTS_ON/.test(src));
  // Pin the actual filter expression inside getCommands(), not just the
  // token's presence anywhere in the file: when OP_QUESTS_ON is falsy,
  // STATIC_COMMANDS must be filtered to drop the href:/quests entry.
  const getCommandsBody = (function () {
    const m = /function getCommands\(\)\s*\{([\s\S]*?)\n  \}/.exec(src);
    return m ? m[1] : '';
  })();
  check('palette.js: getCommands() branches on window.OP_QUESTS_ON',
    /window\.OP_QUESTS_ON\s*\?/.test(getCommandsBody), getCommandsBody);
  check('palette.js: the falsy branch filters STATIC_COMMANDS to drop href:\\/quests',
    /STATIC_COMMANDS\.filter\(c\s*=>\s*c\.action\s*!==\s*'href:\/quests'\)/.test(getCommandsBody), getCommandsBody);
  check('palette.js: the truthy branch keeps the full STATIC_COMMANDS list',
    /window\.OP_QUESTS_ON\s*\n?\s*\?\s*STATIC_COMMANDS\s*\n?\s*:/.test(getCommandsBody), getCommandsBody);
})();

// ── static contract: hotkeys.js ──────────────────────────────────────
(function testHotkeysJs() {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'hotkeys.js'), 'utf8');
  check("hotkeys.js: handles 'k' chord for quests", /k === 'k'/.test(src));
  check("hotkeys.js: 'k' chord is gated by OP_QUESTS_ON", /k === 'k'[\s\S]{0,40}OP_QUESTS_ON/.test(src));
  check("hotkeys.js: 'k' chord routes to \/quests", /k === 'k'[\s\S]{0,60}\/quests/.test(src));
  check("hotkeys.js: existing 'q' chord (data-quality) is untouched",
    /k === 'q'\)\s*location\.href = '\/data-quality'/.test(src));
})();

// ---- the companion cache survives a timezone difference ------------------
// The stored date is the SERVER's day; the browser may be on another one.
check('a companion answer for the browser\'s own day is fresh',
  QUESTS.companionFresh({ date: '2026-09-12', checkedAt: 0 }, '2026-09-12', Date.now()));
check('a server day ahead of the browser is still fresh when just fetched',
  QUESTS.companionFresh({ date: '2026-09-12', checkedAt: Date.now() }, '2026-09-11', Date.now()));
check('yesterday\'s answer is stale once it is a day old',
  !QUESTS.companionFresh({ date: '2026-09-11', checkedAt: Date.now() - 21 * 3600 * 1000 },
                    '2026-09-12', Date.now()));
check('no answer is never fresh', !QUESTS.companionFresh(null, '2026-09-12', Date.now()));

console.log(FAILURES.length === 0 ? 'ALL PASS' : `${FAILURES.length} FAILURE(S)`);
process.exit(FAILURES.length === 0 ? 0 : 1);
