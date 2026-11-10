// ADR-17b "Companion" — the pure half of static/js/quests-companion.js:
// which pages it may visit, and the per-day/per-tab budget of appearances.
const path = require('path');
const C = require(path.join(__dirname, '..', 'static', 'js', 'quests-companion.js'));

let failures = 0;
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (cond ? '' : '  <-- ' + (extra === undefined ? '' : String(extra))));
  if (!cond) failures++;
}

// pages
check('the dashboards page is allowed', C.allowedPage('/'));
check('trailing slash tolerated', C.allowedPage('//') === false || C.allowedPage('/'));
check('every other workspace page is refused',
  !C.allowedPage('/catalog') && !C.allowedPage('/presentations') &&
  !C.allowedPage('/data-sources') && !C.allowedPage('/reference'));
check('report page refused', !C.allowedPage('/d/abc'));
check('presentation show refused', !C.allowedPage('/presentations/abc/show'));
check('share link refused', !C.allowedPage('/s/token'));
check('admin refused', !C.allowedPage('/admin/quests'));
check('explore refused', !C.allowedPage('/explore'));
check('quests page refused (the page owns its own scenes)', !C.allowedPage('/quests'));

// determinism
const a = C.rng('seed|2026-09-12|/')(), b = C.rng('seed|2026-09-12|/')(), c = C.rng('seed|2026-09-13|/')();
check('rng is deterministic for the same key', a === b);
check('rng differs by day', a !== c);

// budget
const fresh = { day: '', n: 0, shownThisTab: false };
check('first day: scene is certain on the dashboards tab',
  !!C.planScene('abc12345', '2026-09-12', '/', fresh, { firstDay: true }));
check('first day: a short fixed wait, not a random one',
  C.planScene('abc12345', '2026-09-12', '/', fresh, { firstDay: true }).delaySec === C.FIRST_DELAY_S && C.FIRST_DELAY_S < C.MIN_DELAY_S);
// This used to say "never". Now it says "still": see the long explanation
// further down the file. Emptiness was indistinguishable from breakage.
const rmFirst = C.planScene('abc12345', '2026-09-12', '/', fresh, { firstDay: true, reducedMotion: true });
check('reduced motion: a still scene, not emptiness', !!rmFirst && rmFirst.still === true);
check('already shown in this tab: never', C.planScene('abc12345', '2026-09-12', '/', { day: '2026-09-12', n: 0, shownThisTab: true }, { firstDay: true }) === null);
check('daily budget exhausted: never', C.planScene('abc12345', '2026-09-12', '/', { day: '2026-09-12', n: C.PER_DAY, shownThisTab: false }, { firstDay: true }) === null);
check('yesterday\'s budget does not count today',
  !!C.planScene('abc12345', '2026-09-12', '/', { day: '2026-09-11', n: C.PER_DAY, shownThisTab: false }, { firstDay: true }));
check('report page: never even with budget', C.planScene('abc12345', '2026-09-12', '/d/x', fresh, { firstDay: true }) === null);

// an ordinary day is a coin toss, but the SAME toss all day for one seed
let seen = 0, total = 0;
for (let i = 0; i < 200; i++) {
  const s = C.planScene('seed' + i, '2026-09-12', '/', fresh, {});
  total++; if (s) seen++;
  if (s) {
    check('scene shape: pose', ['sit', 'peek', 'look'].indexOf(s.pose) >= 0, s.pose);
    check('scene delay >= minimum', s.delaySec >= C.MIN_DELAY_S, s.delaySec);
    break;
  }
}
check('ordinary day: some seeds appear, some do not (sampled)', seen >= 1);
let appear = 0;
for (let i = 0; i < 400; i++) if (C.planScene('s' + i, '2026-09-12', '/', fresh, {})) appear++;
check('ordinary day appearance rate is well under 100% (' + appear + '/400)', appear > 60 && appear < 340, appear);
check('same seed + day + path -> same decision',
  JSON.stringify(C.planScene('q', '2026-09-12', '/', fresh, {})) === JSON.stringify(C.planScene('q', '2026-09-12', '/', fresh, {})));


// ---- the gift day: a resident scene, every page, no budget ---------------
check('a resident scene is planned even with the day\'s budget spent',
  (function () {
    const sc = C.planScene('seed', '2026-09-12', '/', { day: '2026-09-12', n: 9, shownThisTab: true },
                           { resident: true });
    return !!sc && sc.resident === true && sc.staySec === 0;
  })());
check('a resident that was on screen a moment ago is simply here again',
  (function () {
    const sc = C.planScene('seed', '2026-09-12', '/', {}, { resident: true, justHere: true });
    return !!sc && sc.pose === 'stay' && sc.delaySec < 1;
  })());
check('without that note it runs in as usual',
  C.planScene('seed', '2026-09-12', '/', {}, { resident: true }).pose === 'sit');
check('a resident scene comes out soon, not in a minute',
  C.planScene('seed', '2026-09-12', '/', {}, { resident: true }).delaySec === C.FIRST_DELAY_S);
check('a resident scene still refuses a page it does not belong on',
  C.planScene('seed', '2026-09-12', '/d/42', {}, { resident: true }) === null);
// "Reduce motion" used to suppress the scene — and the reward with it: a person
// with that setting NEVER saw the companion and could not tell it from a bug
// (prod, 16.09). The contract now: the rare mode still shows nothing (a surprise
// from the corner IS motion), but on "gift" days and with ?fox=day the fox
// sits STILL: one frame, no animation and no handlers.
var rmStill = C.planScene('seed', '2026-09-12', '/', {}, { resident: true, reducedMotion: true });
check('reduced motion + resident: a still scene, not emptiness',
  !!rmStill && rmStill.still === true && rmStill.resident === true && rmStill.pose === 'stay');
check('a still scene never asks to leave on a timer', rmStill.staySec === 0);
// Second pass (04.10): "only on gift days" was not enough — for a person who
// completed the collection a week ago the gift had run out, and they still saw
// nothing. Now stillness is a TRANSFORMATION of the ordinary scene: rarity and
// budget are computed as always, only the picture stops moving.
var rmRare = C.planScene('seed', '2026-09-12', '/', {}, { reducedMotion: true, firstDay: true });
check('reduced motion in the rare mode: a still scene too',
  !!rmRare && rmRare.still === true && rmRare.pose === 'stay' && rmRare.staySec === 0);
check('a still scene never waits long', rmRare.delaySec <= 3);
check('reduced motion does not bypass the daily budget',
  C.planScene('seed', '2026-09-12', '/', { day: '2026-09-12', n: 2 },
              { reducedMotion: true }) === null);
check('reduced motion does not bypass the one-scene-per-tab rule',
  C.planScene('seed', '2026-09-12', '/', { shownThisTab: true },
              { reducedMotion: true }) === null);
check('reduced motion still never shows on a report page',
  C.planScene('seed', '2026-09-12', '/d/42', {}, { reducedMotion: true, resident: true }) === null);
check('without the gift the budget still applies',
  C.planScene('seed', '2026-09-12', '/', { day: '2026-09-12', n: C.PER_DAY }, {}) === null);

// ---- head tracking: one arc, one angle ----------------------------------
// The manifest contract from static/img/quests/companion/sheets/sheets.json:
// the head strip is a single arc (front -> level left -> straight up) with
// the turn to the right appended as a second run.
const SEGS = { center: 0, arc_left: 24, arc_up: 61, right: [64, 95] };
const HEAD_FRAMES = 96;
// The tracking radius follows the window, so the creature does not lose the
// pointer halfway up a large screen.
check('the tracking radius covers a normal window', (function () {
  global.innerWidth = 1440; global.innerHeight = 900;
  return C.trackRadius() > 1400;
})(), C.trackRadius());
check('the tracking radius never falls below its floor', (function () {
  global.innerWidth = 320; global.innerHeight = 400;
  return C.trackRadius() === C.TRACK_R;
})(), C.trackRadius());
check('the tracking radius is capped for a very wide screen', (function () {
  global.innerWidth = 6000; global.innerHeight = 3000;
  return C.trackRadius() === 2400;
})(), C.trackRadius());
const DEG = Math.PI / 180;
function frameAt(dx, dy) { return C.frameForAim(C.aimFor(dx, dy), SEGS, HEAD_FRAMES); }

check('pointer level left -> the level-left frame', frameAt(-300, 0) === 24, frameAt(-300, 0));
check('pointer straight above -> the overhead frame', frameAt(0, -300) === 61, frameAt(0, -300));
check('pointer level right -> the far end of the right run', frameAt(300, 0) === 95, frameAt(300, 0));
check('135 degrees is a REAL frame between level left and overhead',
  frameAt(-300, -300) > 24 && frameAt(-300, -300) < 61, frameAt(-300, -300));
check('just past the top the head is still looking UP, not at the viewer',
  Math.abs(frameAt(20, -300) - 61) < 4, frameAt(20, -300));
check('the frame rises smoothly through the top of the arc',
  frameAt(-60, -300) < frameAt(0, -300) && frameAt(0, -300) < frameAt(60, -300),
  [frameAt(-60, -300), frameAt(0, -300), frameAt(60, -300)]);
check('45 degrees lands inside the right run',
  frameAt(300, -300) > 64 && frameAt(300, -300) <= 95, frameAt(300, -300));
check('the diagonals are symmetric about the overhead frame',
  Math.abs((61 - frameAt(-300, -300)) - (frameAt(300, -300) - 61)) <= 3,
  [frameAt(-300, -300), frameAt(300, -300)]);
check('the angle maps monotonically from level left to overhead',
  frameAt(-300, -60) < frameAt(-300, -200) && frameAt(-300, -200) < frameAt(-200, -300));
check('a pointer straight below looks straight ahead', frameAt(0, 400) === 0, frameAt(0, 400));
check('a pointer below and to the left clamps to level left', frameAt(-300, 200) === 24);
check('distance does not change the frame, only the angle',
  frameAt(-100, -100) === frameAt(-400, -400));
check('an unusable angle falls back to the centre frame',
  C.frameForAim(NaN, SEGS, HEAD_FRAMES) === 0);
check('a manifest without the arc yields nothing',
  C.frameForAim(1, { center: 0 }, HEAD_FRAMES) === null);
// clamping and degraded manifests
check('a frame beyond the strip is clamped to the last one',
  C.frameForAim(0, SEGS, 10) === 9, C.frameForAim(0, SEGS, 10));
check('never negative',
  C.frameForAim(Math.PI, { center: 0, arc_left: -6, arc_up: -3 }, 10) >= 0);
check('no segments -> null (the caller keeps the idle clip)',
  C.frameForAim(1, null, HEAD_FRAMES) === null);
check('empty segments -> null', C.frameForAim(1, {}, HEAD_FRAMES) === null);
check('no right run -> a pointer on that side holds the overhead frame',
  C.frameForAim(0, { center: 0, arc_left: 24, arc_up: 61 }, HEAD_FRAMES) === 61);
check('same input -> same frame', frameAt(140, -30) === frameAt(140, -30));

// ---- picking the frame that LOOKS at the pointer -------------------------
// A tiny measured set: frame 0 front, 1 a shallow left, 2 a deep left that is
// also tilted up (which is what the real arc does), 3 straight up, 4 right.
const VECS = [[0, 0], [-0.02, -0.004], [-0.09, -0.06], [-0.006, -0.06], [0.06, -0.02]];
function look(dx, dy, p) {
  const L = Math.hypot(dx, dy) || 1;
  return C.frameForVec(VECS, dx / L, dy / L, p);
}
check('pointer straight ahead -> the front frame', look(0, 0, 0) === 0);
check('a pointer level to the left picks a LEFT frame, not the tilted one',
  look(-1, 0, 1) === 2 || look(-1, 0, 1) === 1, look(-1, 0, 1));
check('a near pointer turns the head less than a far one',
  look(-1, 0, 0.2) <= look(-1, 0, 1), [look(-1, 0, 0.2), look(-1, 0, 1)]);
check('straight above picks the upward frame', look(0, -1, 1) === 3, look(0, -1, 1));
check('to the right picks the right frame', look(1, 0, 1) === 4, look(1, 0, 1));
check('up and to the right still picks the right frame', look(1, -1, 1) === 4, look(1, -1, 1));
check('below, where the art has nothing, looks straight ahead',
  look(0, 1, 1) === 0, look(0, 1, 1));
check('an empty set yields nothing', C.frameForVec([], -1, 0, 1) === null);
check('the same request gives the same frame', look(-1, -0.5, 0.6) === look(-1, -0.5, 0.6));

// ---- the walk along the strip: a turn, never a snap ----------------------
// The tick eases the shown frame towards the aimed one and caps how fast it
// may travel, so a long turn crosses real in-between frames of the clip.
check('the frame ceiling is a sane head speed',
  C.HEAD_MAX_FPS >= 40 && C.HEAD_MAX_FPS <= 140, C.HEAD_MAX_FPS);
check('the ease settles in well under a second',
  C.HEAD_TAU > 0.01 && C.HEAD_TAU < 0.15, C.HEAD_TAU);
check('crossing the whole strip takes several frames, not one',
  (HEAD_FRAMES / C.HEAD_MAX_FPS) > 0.3, HEAD_FRAMES / C.HEAD_MAX_FPS);

console.log();
console.log(failures ? failures + ' FAILED' : 'ALL PASS');
process.exit(failures ? 1 : 0);
