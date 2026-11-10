/* Headless harness for the pure helpers in static/js/dashboards.js.
 *
 * dashboards.js touches a lot of DOM at load time (filters, folder popover,
 * grid builder, IntersectionObserver…), so — same fallback the onepage-testing
 * skill sanctions for explore.js-style modules — the pure logic (sectionize,
 * isStale, fmtDM, the favourite fetch helpers) is factored to the top of the
 * file and exported via `module.exports` BEFORE the script touches `document`,
 * guarded by `if (typeof document === 'undefined') return;`. Requiring the
 * module with no `document` global in scope runs only that pure block.
 *
 * Run: node tests/dashboards_dom.js   (prints PASS/FAIL lines + ALL PASS)
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

const D = require(path.join(ROOT, 'static', 'js', 'dashboards.js'));

// ── sectionize ───────────────────────────────────────────────────────
(function () {
  const items = [
    { id: 'a', fav: null },
    { id: 'b', fav: 2 },
    { id: 'c', fav: null },
    { id: 'd', fav: 1 },
  ];
  const { favorites, others } = D.sectionize(items);
  check('sectionize: favorites sorted by position', favorites.map(i => i.id).join(',') === 'd,b',
        favorites.map(i => i.id).join(','));
  check('sectionize: others keep original order, exclude favourites',
        others.map(i => i.id).join(',') === 'a,c', others.map(i => i.id).join(','));

  const empty = D.sectionize([]);
  check('sectionize: empty input -> empty both', empty.favorites.length === 0 && empty.others.length === 0);
  const none = D.sectionize(undefined);
  check('sectionize: undefined input -> empty both (no throw)', none.favorites.length === 0 && none.others.length === 0);
})();

// ── isStale ──────────────────────────────────────────────────────────
(function () {
  const today = '2026-08-22';
  const eightDaysAgo = '2026-08-14';   // 8 days before today
  const threeDaysAgo = '2026-08-19';   // 3 days before today
  const sevenDaysAgo = '2026-08-15';   // exactly 7 days: not stale (> 7, not >=)
  check('isStale: 8 days old -> stale', D.isStale(eightDaysAgo, today) === true);
  check('isStale: 3 days old -> not stale', D.isStale(threeDaysAgo, today) === false);
  check('isStale: exactly 7 days old -> not stale', D.isStale(sevenDaysAgo, today) === false);
  check('isStale: no date -> not stale', D.isStale(null, today) === false);
  check('isStale: today -> not stale', D.isStale(today, today) === false);
})();

// ── fmtDM ────────────────────────────────────────────────────────────
(function () {
  check('fmtDM: YYYY-MM-DD -> DD.MM', D.fmtDM('2026-08-22') === '22.08', D.fmtDM('2026-08-22'));
  check('fmtDM: null -> null', D.fmtDM(null) === null);
})();

// ── favoriteUrl ──────────────────────────────────────────────────────
(function () {
  check('favoriteUrl: builds the PUT/DELETE target',
        D.favoriteUrl('abc-123') === '/api/dashboards/abc-123/favorite', D.favoriteUrl('abc-123'));
})();

// ── setFavorite / saveFavoritesOrder: fetch shape ───────────────────
(async function () {
  let calls = [];
  global.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true };
  };

  calls = [];
  const okOn = await D.setFavorite('dash-1', true);
  check('setFavorite(true): resolves ok', okOn === true);
  check('setFavorite(true): PUT method', calls[0].opts.method === 'PUT', calls[0].opts.method);
  check('setFavorite(true): correct url', calls[0].url === '/api/dashboards/dash-1/favorite', calls[0].url);
  check('setFavorite(true): JSON content-type header',
        calls[0].opts.headers['Content-Type'] === 'application/json', JSON.stringify(calls[0].opts.headers));

  calls = [];
  const okOff = await D.setFavorite('dash-1', false);
  check('setFavorite(false): DELETE method', calls[0].opts.method === 'DELETE', calls[0].opts.method);
  check('setFavorite(false): JSON content-type header still set',
        calls[0].opts.headers['Content-Type'] === 'application/json');
  check('setFavorite(false): resolves ok', okOff === true);

  calls = [];
  await D.saveFavoritesOrder(['x', 'y', 'z']);
  check('saveFavoritesOrder: PUT to /api/favorites/order',
        calls[0].url === '/api/favorites/order' && calls[0].opts.method === 'PUT', JSON.stringify(calls[0]));
  check('saveFavoritesOrder: JSON body carries ids',
        JSON.parse(calls[0].opts.body).ids.join(',') === 'x,y,z', calls[0].opts.body);
  check('saveFavoritesOrder: JSON content-type header',
        calls[0].opts.headers['Content-Type'] === 'application/json');

  // A failed request surfaces as false, not a throw.
  global.fetch = async () => ({ ok: false });
  const failOk = await D.setFavorite('dash-1', true);
  check('setFavorite: !res.ok -> false', failOk === false);

  delete global.fetch;

  console.log();
  if (FAILURES.length) {
    console.log(`${FAILURES.length} FAILED: ` + FAILURES.join(', '));
    process.exit(1);
  }
  console.log('ALL PASS');
})();
