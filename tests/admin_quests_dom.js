/* Headless harness for templates/pages/admin_quests.html + static/js/admin-quests.js.
 *
 * No jsdom: the template is checked as text (id contract, no `| safe`, tabbar
 * link), and admin-quests.js's pure helpers (parseHolidays/validateBankText)
 * are exercised by requiring the module directly — it exports before
 * touching `document`, so this works with zero DOM (same idiom as
 * static/js/explore.js's `module.exports` guard, tests/explore_dom.js).
 *
 * Run: node tests/admin_quests_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

// ── template: id contract + tabbar ──────────────────────────────────────
const TPL = fs.readFileSync(path.join(ROOT, 'templates/pages/admin_quests.html'), 'utf8');

const REQUIRED_IDS = [
  'qa-enabled-toggle', 'qa-season-start', 'qa-season-length',
  'qa-status-save', 'qa-status-msg',
  'qa-holidays', 'qa-holidays-save', 'qa-holidays-status',
  'qa-bank-json', 'qa-bank-file', 'qa-bank-check', 'qa-bank-import',
  'qa-bank-default', 'qa-bank-report',
];
for (const id of REQUIRED_IDS) {
  const re = new RegExp('id="' + id + '"');
  check('template has id="' + id + '"', re.test(TPL));
}

check('template has no `| safe` filter (all user/server text via Jinja auto-escape)',
  !/\|\s*safe\b/.test(TPL), '`| safe` found');

// The label is extracted for i18n, so the template holds the {{ _('…') }} call
// and the English text is the catalogue key inside it.
check('tabbar links to /admin/quests with «Quests»',
  /<a class="tab active" href="\/admin\/quests">\{\{ _\('Quests'\) \}\}<\/a>/.test(TPL));

check('extends base.html', /\{%\s*extends\s*"base\.html"\s*%\}/.test(TPL));
check('sets active = admin', /\{%\s*set\s*active\s*=\s*'admin'\s*%\}/.test(TPL));
check('loads admin-quests.js in scripts block',
  /static\/js\/admin-quests\.js\?v=\{\{\s*asset_v\s*\}\}/.test(TPL));

// ── tabbar propagated to the other admin pages ──────────────────────────
for (const f of ['admin.html', 'admin_employees.html', 'admin_generators.html']) {
  const full = path.join(ROOT, 'templates/pages', f);
  if (!fs.existsSync(full)) { check('tabbar in ' + f + ' links to Quests', false, 'file missing'); continue; }
  const html = fs.readFileSync(full, 'utf8');
  check('tabbar in ' + f + ' links to /admin/quests',
    /<a class="tab" href="\/admin\/quests">\{\{ _\('Quests'\) \}\}<\/a>/.test(html));
}

// ── static/js/admin-quests.js: pure helpers ─────────────────────────────
const AQ = require(path.join(ROOT, 'static', 'js', 'admin-quests.js'));

check('admin-quests.js exports parseHolidays', typeof AQ.parseHolidays === 'function');
check('admin-quests.js exports validateBankText', typeof AQ.validateBankText === 'function');

// parseHolidays
{
  const r = AQ.parseHolidays('2026-01-01\n2026-03-08\n2026-01-01\n\n2026-01-01');
  check('parseHolidays dedupes and sorts', JSON.stringify(r.dates) === JSON.stringify(['2026-01-01', '2026-03-08']), JSON.stringify(r));
  check('parseHolidays: no errors on valid input', r.errors.length === 0, JSON.stringify(r.errors));
}
{
  const r = AQ.parseHolidays('2026-13-40\nnot-a-date\n2026-05-01');
  check('parseHolidays flags bad lines', r.errors.length === 2, JSON.stringify(r));
  check('parseHolidays keeps the valid line', JSON.stringify(r.dates) === JSON.stringify(['2026-05-01']), JSON.stringify(r));
}
{
  const r = AQ.parseHolidays('  2026-02-23  \n\n\n2026-02-23');
  check('parseHolidays trims whitespace and ignores blank lines',
    JSON.stringify(r.dates) === JSON.stringify(['2026-02-23']) && r.errors.length === 0, JSON.stringify(r));
}
{
  const r = AQ.parseHolidays('2026-02-30'); // not a real calendar date (Feb has 28/29 days)
  check('parseHolidays rejects a calendar-invalid date', r.errors.length === 1 && r.dates.length === 0, JSON.stringify(r));
}
{
  const r = AQ.parseHolidays('');
  check('parseHolidays on empty text: no dates, no errors', r.dates.length === 0 && r.errors.length === 0, JSON.stringify(r));
}

// validateBankText
{
  const r = AQ.validateBankText('[{"id":1,"day":1,"q":"?","o":["a","b","c","d"],"a":0,"e":"","t":"x"}]');
  check('validateBankText accepts a JSON array', r.ok === true && Array.isArray(r.bank) && r.bank.length === 1, JSON.stringify(r));
}
{
  const r = AQ.validateBankText('not json at all {');
  check('validateBankText rejects invalid JSON', r.ok === false && r.bank === null && !!r.error, JSON.stringify(r));
}
{
  const r = AQ.validateBankText('{"id":1}'); // valid JSON, but an object not a list
  check('validateBankText rejects a non-array JSON value', r.ok === false && r.bank === null, JSON.stringify(r));
}
{
  const r = AQ.validateBankText('   ');
  check('validateBankText rejects blank text', r.ok === false, JSON.stringify(r));
}
{
  const r = AQ.validateBankText('[]');
  check('validateBankText accepts an empty array (server owns count/day checks)', r.ok === true && Array.isArray(r.bank) && r.bank.length === 0, JSON.stringify(r));
}

// ── static contract: 422 `detail` handling (finding 18) ─────────────────
// The server's 422 body now carries the FULL report object as `detail`
// ({ok,count,days,errors[],dry_run}), not a bare list. admin-quests.js must
// read detail.errors/.count/.days, with a fallback for a plain-list detail
// (older server / generic FastAPI validation error).
(function testDetailNormalization() {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'admin-quests.js'), 'utf8');
  check('admin-quests.js reads detail.errors from a 422 report object',
    /detail\.errors/.test(src));
  check('admin-quests.js reads detail.count from a 422 report object',
    /detail\.count/.test(src));
  check('admin-quests.js reads detail.days from a 422 report object',
    /detail\.days/.test(src));
  check('admin-quests.js falls back to a plain-list detail shape',
    /Array\.isArray\(detail\)/.test(src));
  check('admin-quests.js no longer assumes detail is always an array before reading it',
    !/res\.status === 422 && (body|report) && Array\.isArray\((body|report)\.detail\)/.test(src));
})();

// ── done ─────────────────────────────────────────────────────────────
if (FAILURES.length) {
  console.log(`\n${FAILURES.length} FAILURE(S):`);
  FAILURES.forEach(f => console.log('  - ' + f));
  process.exit(1);
} else {
  console.log('\nALL PASS');
}
