/* Run panel folders: the report's folders are listed from the server and a
 * stored folder is never dropped on restore (prod 2026-08-23: extras
 * "reset by themselves" after a queryPermission hiccup).
 *   node tests/gen_folders_dom.js */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub
const path = require('path');
const { restoredExtra, missingExpected } = require(path.join(__dirname, '..', 'static', 'js', 'gen-engine.js'));
const F = [];
function check(l, c, x) { console.log((c ? 'PASS ' : 'FAIL ') + l + (!c && x ? '  <-- ' + x : '')); if (!c) F.push(l); }

const h = { name: 'SL NWD' };
check('restore: granted -> ready', restoredExtra({ h, w: false }, 'granted').ready === true);
check('restore: prompt -> kept, not ready', restoredExtra({ h, w: true }, 'prompt').ready === false);
check('restore: DENIED -> still kept (Reconnect asks again), not ready',
  restoredExtra({ h, w: false }, 'denied').h === h && restoredExtra({ h, w: false }, 'denied').ready === false);
check('restore: the write flag survives', restoredExtra({ h, w: true }, 'denied').w === true);

check('slots: every expected folder is listed when nothing is connected',
  missingExpected(['2026', 'SL NWD'], []).join(',') === '2026,SL NWD');
check('slots: a connected folder leaves the list', missingExpected(['2026', 'SL NWD'], ['2026']).join(',') === 'SL NWD');
check('slots: nothing expected -> nothing listed', missingExpected([], ['x']).length === 0);
check('slots: undefined inputs are safe', missingExpected(undefined, undefined).length === 0);
console.log(); if (F.length) { console.log(F.length + ' FAILED'); process.exit(1); } console.log('ALL PASS');
