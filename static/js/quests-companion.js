/* OnePage — quests-companion.js (ADR-17b "Companion")
 *
 * The creature behind the full 8-card collection. Loaded lazily by
 * static/js/quests.js ONLY when the once-a-day badge answer (or the /quests
 * page) said this person has it and has not switched it off — everyone else
 * never fetches this file, its manifest or its images.
 *
 * Rules that keep it grown-up (owner decisions 2026-09-11): it never speaks,
 * never makes a sound, appears at most once per page load and twice a day,
 * not before 40 s after load, never while an overlay/palette is open, runs
 * away when one opens, leaves after a click anywhere, and does nothing at
 * all under prefers-reduced-motion. It lives only on workspace pages —
 * never on a report, a presentation, a share link or admin screens.
 *
 * MOTION (2026-09-11, owner: "it moves crookedly, it runs out badly", "it does not watch
 * the cursor"): the creature is no longer a PNG pushed around by CSS
 * transforms. It is frame animation played from horizontal sprite strips
 * described by static/img/quests/companion/sheets/sheets.json — ONE fetch,
 * issued only after the page gate, only for an owner with a planned scene.
 * The player below advances frames on requestAnimationFrame (not CSS
 * `steps()`) because the head has to be PAUSED and SEEKED frame by frame to
 * follow the pointer. If the manifest is missing, malformed, or a clip is
 * absent, the module silently falls back to the original single-PNG poses.
 *
 * Everything above the `typeof document` bail-out is pure and exported for
 * node tests (tests/quests_companion_dom.js).
 */
(function () {
  'use strict';

  // The Dashboards page only. The corner it sits in is the corner of the
  // WINDOW, and on pages whose content reaches that corner it ends up sitting
  // on someone's data; the dashboards list always has room below it (owner,
  // 2026-09-12: "then let's keep her on the Dashboard page"). Duplicated
  // in quests.js — that copy is what keeps every other page from loading this
  // module at all.
  var WORKSPACE = ['/'];
  // The very first appearance is the one the person is waiting for, so it
  // comes almost at once; later days keep the rare, quiet cadence (owner,
  // stand 2026-09-11: "it takes too long for her to appear the first time").
  var MIN_DELAY_S = 40, MAX_DELAY_S = 110, FIRST_DELAY_S = 2;
  var STAY_S = 28;             // how long a scene stays before it leaves on its own
  var SLEEP_AFTER_S = 180;     // no input for this long while visible -> sleeps
  var PER_DAY = 2;
  // px: the pointer is «near» inside this radius. Wide enough to cover the
  // working area a person's cursor actually crosses — at 320 px the creature
  // lost the pointer a third of the way up the window and went back to facing
  // front, which reads as it losing interest mid-glance (owner, stand
  // 2026-09-12; colleague's review: far targets should CLAMP, not drop).
  var TRACK_R = 520;           // the floor, for a narrow window
  // How far the creature will follow the pointer. Derived from the window, not
  // a constant: from its corner the working area is most of the width, and a
  // fixed 320 px meant it lost the pointer a third of the way up and went back
  // to facing front, which reads as losing interest mid-glance (owner, stand
  // 2026-09-12; colleague's review: a far target should CLAMP, not drop). The
  // gaze clamps at the end of the arc instead.
  function trackRadius() {
    var w = (typeof innerWidth === 'number' && innerWidth > 0) ? innerWidth : 1280;
    var h = (typeof innerHeight === 'number' && innerHeight > 0) ? innerHeight : 800;
    return Math.max(TRACK_R, Math.min(2400, Math.round(Math.sqrt(w * w + h * h))));
  }
  var TRACK_LOST_MS = 700;     // pointer gone this long -> ease back to centre
  var HEAD_SPEED = 90;         // frames a second while following the pointer
  var HEAD_TAU = 0.038;        // s: the head eases onto the pointer, never snaps
  // And a ceiling in the STRIP's own frames: the two gaze runs are different
  // lengths and sit apart in the strip, so the same gaze step can mean four
  // frames or forty. Without this the head crosses half the strip in one
  // animation frame and reads as a snap.
  var HEAD_MAX_FPS = 70;

  function allowedPage(path) {
    var p = String(path || '/');
    if (p.length > 1 && p.charAt(p.length - 1) === '/') p = p.slice(0, -1);
    return WORKSPACE.indexOf(p) >= 0;
  }

  // Small deterministic generator (mulberry32) seeded from the seed string
  // and the day: the same person sees the same decision all day in every
  // tab, so two tabs cannot "double" the daily budget by racing.
  function hash32(str) {
    var h = 2166136261 >>> 0;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    return h >>> 0;
  }
  function rng(seedStr) {
    var a = hash32(String(seedStr));
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      var x = a;
      x = Math.imul(x ^ (x >>> 15), x | 1);
      x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
      return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
    };
  }

  function dayKey(d) {
    d = d || new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  // Decide the scene for this page load, or null. `counters` = {day, n,
  // shownThisTab}. `firstDay` = the first day after the reveal: the creature
  // shows up for sure on the Dashboards tab (the "next morning" moment).
  // "Reduce motion" used to mean "do not show at all" — and a person with that
  // setting NEVER saw the reward, with nothing to tell it apart from a breakage.
  // The first fix (16.09) brought back a still pose only for the "gift"
  // day, and that was not enough: for someone who completed the collection a week
  // ago the gift had long expired, and they still saw nothing (prod,
  // 04.10, a second person with the same symptom). So the decision is made
  // ONCE and as a general rule: budget, dice and rarity are computed as
  // usual, and at the end the scene is turned into a still one. ADR-17b promises
  // "one still pose", not emptiness.
  function stillify(scene) {
    if (!scene) return null;
    scene.pose = 'stay';          // no running in, no peeking from behind the edge
    scene.still = true;           // no breathing, no head turns
    scene.staySec = 0;            // leaving is motion too: it just sits until navigation
    if (scene.delaySec > 3) scene.delaySec = 3;
    return scene;
  }

  function planScene(seed, day, path, counters, opts) {
    opts = opts || {};
    if (!allowedPage(path)) return null;
    if (opts.reducedMotion) return stillify(planMoving(seed, day, path, counters, opts));
    return planMoving(seed, day, path, counters, opts);
  }

  function planMoving(seed, day, path, counters, opts) {
    // «Lives here» — set for a day at a time, for watching it properly. It
    // comes out on every visit to a workspace page, soon, and stays until the
    // page is left. The budget and the dice do not apply.
    if (opts.resident) {
      // Moving between sections of the app is a full page load, so the element
      // cannot survive it. If it was on screen moments ago, it simply IS here
      // — no delay, no running in again — and the move reads as it having sat
      // there the whole time (owner, 2026-09-12: "I thought she would
      // sit on top of the tabs").
      // It runs in ONCE. After that it is simply there whenever the page is
      // opened: the entrance is an event, and an event that happens on every
      // page load is not one (owner, 2026-09-12: "let it run out once,
      // not every time").
      if (opts.justHere || opts.entered) {
        return { pose: 'stay', delaySec: 0.15, staySec: 0, resident: true };
      }
      return { pose: 'sit', delaySec: FIRST_DELAY_S, staySec: 0, resident: true };
    }
    if (counters && counters.shownThisTab) return null;
    var n = (counters && counters.day === day) ? (counters.n || 0) : 0;
    if (n >= PER_DAY) return null;
    var r = rng(seed + '|' + day + '|' + path);
    var pAppear = opts.firstDay ? 1 : 0.45;
    if (r() > pAppear) return null;
    var delay = Math.round(MIN_DELAY_S + r() * (MAX_DELAY_S - MIN_DELAY_S));
    var pose = (path === '/' && r() < 0.6) ? 'look' : (r() < 0.5 ? 'sit' : 'peek');
    return { pose: pose, delaySec: opts.firstDay ? FIRST_DELAY_S : delay, staySec: STAY_S };
  }

  // ---- head tracking, pure ------------------------------------------------
  // The `head` strip is one continuous turn: the extreme LEFT look at one end
  // of `segments.left`, the extreme RIGHT at one end of `segments.right`, the
  // straight-ahead frame at `segments.center`, and an upward glance somewhere
  // in `segments.up`. Which END of a segment sits next to the centre frame is
  // not stated by the contract, so it is derived: the endpoint nearer to the
  // centre frame is the shallow turn, the far endpoint the extreme one. That
  // makes the mapping correct for both [0, 14] and [14, 0] orderings.
  function segEnds(s) {
    if (typeof s === 'number' && isFinite(s)) return [s, s];
    if (s && typeof s.length === 'number' && s.length >= 2 &&
        isFinite(+s[0]) && isFinite(+s[1])) return [+s[0], +s[1]];
    return null;
  }
  function ramp(ends, centre, p) {
    var a = ends[0], b = ends[1];
    var near = Math.abs(a - centre) <= Math.abs(b - centre) ? a : b;
    var far = near === a ? b : a;
    return near + p * (far - near);
  }
  // Target frame of the `head` clip for a pointer at (dx, dy) from the
  // sprite's head centre (screen coordinates: dy < 0 means ABOVE). Returns
  // null when the manifest carries no usable segments — the caller then keeps

  // The angle from the creature's eye to the pointer, in radians: 0 is level
  // right, PI/2 straight up, PI level left. A pointer BELOW the eye clamps to
  // whichever end it is nearer, because the strip has nothing below level.
  function aimFor(dx, dy) {
    dx = +dx; dy = +dy;
    if (!isFinite(dx)) dx = 0;
    if (!isFinite(dy)) dy = 0;
    var th = Math.atan2(-dy, dx);
    // Below the eye the strip has nothing: clamp to the level end on that
    // side, or — for a pointer straight underneath — to straight ahead.
    if (th < 0) {
      if (Math.abs(dx) < 40) return NaN;
      th = dx < 0 ? Math.PI : 0;
    }
    return th;
  }
  // Pick the frame that LOOKS nearest a given direction.
  //
  // `vecs` is the measured gaze of every frame: where the eyes and nose sit
  // relative to the front-facing frame. `ux,uy` is the unit direction from the
  // creature's eyes to the pointer (y negative = up), `p` how far to turn,
  // 0..1. The target is p of the furthest this set can look along that
  // direction, and the frame chosen is the closest to it — with error ACROSS
  // the direction weighted heavier than error along it, because pointing the
  // right WAY matters more than turning the exact amount.
  //
  // This replaces marking «level left» and «straight up» by hand. The arc in
  // the art never reaches level left — its flattest frame is 14 degrees above
  // the horizon — so a hand-marked «level» frame made the creature look well
  // above a pointer that was level with it (owner, 2026-09-12).
  var GAZE_PERP_W = 2.5;
  function frameForVec(vecs, ux, uy, p) {
    if (!vecs || !vecs.length) return null;
    var i, v, pr, rmax = 0;
    for (i = 0; i < vecs.length; i++) {
      v = vecs[i];
      pr = v[0] * ux + v[1] * uy;
      if (pr > rmax) rmax = pr;
    }
    if (!(rmax > 0)) return 0;                    // nothing looks that way
    p = Math.min(1, Math.max(0, p));
    var tx = ux * rmax * p, ty = uy * rmax * p;
    var best = 0, bestScore = Infinity;
    for (i = 0; i < vecs.length; i++) {
      v = vecs[i];
      var ax = v[0] - tx, ay = v[1] - ty;
      var along = ax * ux + ay * uy;
      var perp = ax * uy - ay * ux;
      var sc = along * along + GAZE_PERP_W * perp * perp;
      if (sc < bestScore) { bestScore = sc; best = i; }
    }
    return best;
  }

  // The frame for that angle. The head strip is ONE arc — front, round to
  // level left, up and over to straight up — so every angle in between has a
  // real frame and a diagonal is not an approximation of two others (owner,
  // stand 2026-09-12: "45 degrees do not work (135 degrees)"). The turn to
  // the right is a second run, reached past the top of the arc.
  function frameForAim(th, segments, frames) {
    if (!segments || typeof segments !== 'object') return null;
    var up = +segments.arc_up, lf = +segments.arc_left;
    if (!isFinite(up) || !isFinite(lf)) return null;
    var c = isFinite(+segments.center) ? +segments.center : 0;
    var right = segEnds(segments.right);
    var n = Math.max(0, frames | 0);
    function clampF(v) {
      if (!isFinite(v)) return c;
      if (v < 0) v = 0;
      if (n > 0 && v > n - 1) v = n - 1;
      return v;
    }
    th = +th;
    if (!isFinite(th)) return clampF(c);
    var half = Math.PI / 2;
    if (th >= half) {
      var u = Math.min(1, (th - half) / half);   // 0 up, 1 level left
      return clampF(up + (lf - up) * u);
    }
    var v = Math.min(1, (half - th) / half);     // 0 up, 1 level right
    if (!right) return clampF(up);
    // The right-hand run starts from the FRONT pose, not from the overhead
    // one, so it must not be entered at the top of the arc: at 89 degrees the
    // head jumped from looking up to looking at the viewer (owner, stand
    // 2026-09-12: "at 93 degrees it loses the mouse and looks at you"). The whole
    // stretch from overhead to level right is one run of frames — up, past the
    // front, round to the right — so map the angle across ALL of it.
    var far = Math.max(right[0], right[1]);
    return clampF(up + (far - up) * v);
  }



  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { allowedPage: allowedPage, planScene: planScene, rng: rng, dayKey: dayKey,
                       PER_DAY: PER_DAY, MIN_DELAY_S: MIN_DELAY_S, FIRST_DELAY_S: FIRST_DELAY_S,
                       TRACK_R: TRACK_R, trackRadius: trackRadius,
                       aimFor: aimFor, frameForAim: frameForAim, frameForVec: frameForVec,
                       HEAD_TAU: HEAD_TAU, HEAD_MAX_FPS: HEAD_MAX_FPS };
  }
  if (typeof document === 'undefined') return;
  if (window.self !== window.top) return;

  var INFO = window.OP_COMPANION || null;
  if (!INFO || !INFO.seed || INFO.enabled === false) return;
  var UID = String((window.OP_USER || {}).person_id || 'anon');
  var reduced = false;
  try { reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}

  var ASSET_V = (function () {
    var s = document.querySelector('script[src*="quests.js?v="]');
    var m = s && /v=([^&]+)/.exec(s.getAttribute('src'));
    return m ? m[1] : '0';
  })();
  var BASE = '/static/img/quests/companion/';
  function img(pose) { return BASE + pose + '.png?v=' + ASSET_V; }
  // The manifest names the file to load (`file`), so the pipeline may ship
  // WebP — a 42-frame 192 px strip is 1.6 MB as PNG and 240 KB as WebP, and
  // these are fetched by the few people who completed the collection.
  // Without that field the historical `<clip>.png` name still works.
  function sheetUrl(clip) {
    var m = SHEETS && SHEETS[clip];
    var file = (m && typeof m.file === 'string' && m.file) ? m.file : (clip + '.png');
    return BASE + 'sheets/' + file + '?v=' + ASSET_V;
  }

  var DAY_KEY = 'op-companion-day-' + UID;
  var TAB_KEY = 'op-companion-tab-' + UID;
  function readCounters() {
    var c = { day: '', n: 0, shownThisTab: false };
    try { var raw = localStorage.getItem(DAY_KEY); if (raw) { var o = JSON.parse(raw); c.day = o.day; c.n = o.n | 0; } } catch (e) {}
    try { c.shownThisTab = !!sessionStorage.getItem(TAB_KEY); } catch (e) {}
    return c;
  }
  function noteShown(day) {
    try {
      var c = readCounters();
      localStorage.setItem(DAY_KEY, JSON.stringify({ day: day, n: (c.day === day ? c.n : 0) + 1 }));
    } catch (e) {}
    try { sessionStorage.setItem(TAB_KEY, '1'); } catch (e) {}
  }

  var today = dayKey();
  // The first day after the collection is completed is a GIFT: the creature
  // simply lives on the workspace pages — it comes out on every page, stays
  // while the person is there, and the daily budget does not apply. It goes
  // back to being rare and quiet when the day is up (owner, 2026-09-12:
  // "the fox cub runs away forever… could it sit for at least a day? like
  // a gift"). The clock is the server's stamp, so it is the same in every
  // browser and every tab.
  var GIFT_MS = 24 * 3600 * 1000;
  var giftDay = false;
  var firstDay = false;
  try {
    if (INFO.since) {
      var sinceMs = Date.parse(INFO.since + (INFO.since.indexOf('Z') < 0 ? 'Z' : ''));
      if (isFinite(sinceMs)) giftDay = (Date.now() - sinceMs) < GIFT_MS;
    }
  } catch (e) {}
  try {
    if (INFO.since) {
      var sinceDay = dayKey(new Date(INFO.since + (INFO.since.indexOf('Z') < 0 ? 'Z' : '')));
      // Certain while fewer than three scenes were actually SHOWN (a person
      // who left before the fox came out has not seen it), with a cap of
      // eight plans so an early-leaver is not promised one forever.
      var shown = parseInt(localStorage.getItem('op-companion-firstday-shown-' + UID) || '0', 10) || 0;
      var planned = parseInt(localStorage.getItem('op-companion-firstday-' + UID) || '0', 10) || 0;
      firstDay = !!sinceDay && shown < 3 && planned < 8;
    }
  } catch (e) {}

  // `?fox=day` keeps it on the page for the next 24 hours; `?fox=off` ends
  // that early. Per browser, and it never touches anyone else.
  var RESIDENT_KEY = 'op-companion-resident-' + UID;
  var resident = false;
  try {
    var qq = String(location.search || '');
    if (qq.indexOf('fox=day') >= 0) localStorage.setItem(RESIDENT_KEY, String(Date.now() + 24 * 3600 * 1000));
    else if (qq.indexOf('fox=off') >= 0) localStorage.removeItem(RESIDENT_KEY);
    var until = parseInt(localStorage.getItem(RESIDENT_KEY) || '0', 10) || 0;
    if (until && until > Date.now()) resident = true;
    else if (until) localStorage.removeItem(RESIDENT_KEY);
  } catch (e) {}
  if (giftDay) resident = true;
  // A short-lived note that it was on screen: written while it is here and
  // read on the next page. sessionStorage, so it never outlives the tab.
  var HERE_KEY = 'op-companion-here-' + UID;
  var HERE_FRESH_MS = 12000;
  var justHere = false, hereFrame = 0, herePtr = null;
  // Whether it has already made its entrance today.
  // Keyed by the day AND by the reveal stamp: re-granting the collection
  // (a reset on a stand) replays the entrance, instead of the browser
  // insisting it has already seen it today.
  var ENTERED_KEY = 'op-companion-entered-' + UID;
  var ENTERED_VAL = today + '|' + String(INFO.since || '');
  var enteredToday = false;
  try { enteredToday = localStorage.getItem(ENTERED_KEY) === ENTERED_VAL; } catch (e) {}
  function noteEntered() {
    try { localStorage.setItem(ENTERED_KEY, ENTERED_VAL); } catch (e) {}
  }
  try {
    var raw = String(sessionStorage.getItem(HERE_KEY) || '');
    var bits = raw.split(':');
    var wasAt = parseInt(bits[0] || '0', 10) || 0;
    hereFrame = parseInt(bits[1] || '0', 10) || 0;
    var hx = parseInt(bits[2] || '-1', 10), hy = parseInt(bits[3] || '-1', 10);
    if (isFinite(hx) && hx >= 0 && isFinite(hy) && hy >= 0) herePtr = { x: hx, y: hy };
    justHere = !!wasAt && (Date.now() - wasAt) < HERE_FRESH_MS;
  } catch (e) {}
  function noteHere() {
    var f = 0;
    try { if (player && player.clip() === 'idle') f = player.frame() | 0; } catch (e2) {}
    var px = ptr ? Math.round(ptr.x) : -1, py = ptr ? Math.round(ptr.y) : -1;
    try { sessionStorage.setItem(HERE_KEY, Date.now() + ':' + f + ':' + px + ':' + py); } catch (e) {}
  }
  function forgetHere() {
    try { sessionStorage.removeItem(HERE_KEY); } catch (e) {}
  }

  // A scene can be still for one reason only — "reduce
  // motion". The flag is raised once and then queried instead of the media
  // query: that makes it visible that stillness follows from the plan, not from
  // checks scattered across the module.
  var STILL = false;
  var scene = planScene(INFO.seed, today, location.pathname, readCounters(),
                        { reducedMotion: reduced, firstDay: firstDay, resident: resident,
                          justHere: justHere, entered: enteredToday });
  if (!scene) return;
  STILL = !!scene.still;
  // The «first day» is spent the moment it is planned — a person who leaves
  // the page before the 40 s must not get a certain scene on every load.
  if (firstDay) {
    try {
      var k = 'op-companion-firstday-' + UID;
      localStorage.setItem(k, String((parseInt(localStorage.getItem(k) || '0', 10) || 0) + 1));
    } catch (e) {}
  }

  // ---- the sprite-strip player -------------------------------------------
  // play(clip, {loop, onEnd, from, to}) | seek(clip, frame) | stop()
  // Frames advance on requestAnimationFrame at the clip's own fps, so the
  // clip can be paused and seeked (cursor tracking) and played once or looped
  // without touching the CSS animation timeline.
  function createSpritePlayer(node, sheets, boxOf) {
    var cur = null, frame = 0, from = 0, to = 0, fpsAcc = 0, last = 0;
    // ONE mirror pivot for every clip. The clips share a crop, so the
    // creature is already aligned across them; taking `cx` from whichever
    // clip happens to be playing made the body hop on every clip switch
    // (owner, stand 2026-09-11: "when you start petting the fox it
    // jumps somewhere by its centre").
    var cxRef = 0.5, byRef = 1;
    var playing = false, looping = false, ended = null, raf = 0, dead = false, tickCb = null;
    var pingpong = false, dir = 1, waiting = null;
    // The <img> that carries the strip. It lives inside `node`, which clips it
    // to one cell.
    // ONE <img> PER SHEET, never a src swap. Assigning a new src empties the
    // element until the new image has loaded and decoded — even from cache —
    // so the creature vanished for a tenth of a second at every clip change,
    // and was caught half-drawn in between (owner's screen recording,
    // 2026-09-11, 4.15 s: right half missing; 4.20-4.25 s: nothing but the
    // shadow). Each sheet gets its own element, and switching clips only
    // changes which one is visible.
    var films = {};
    var film = null;
    function filmFor(name) {
      var f = films[name];
      if (f) return f;
      var m = meta(name);
      if (!m) return null;
      f = document.createElement('img');
      f.className = 'op-comp-film';
      f.alt = '';
      f.draggable = false;
      f.decoding = 'sync';
      f.style.visibility = 'hidden';
      f.src = sheetUrl(name);
      node.appendChild(f);
      films[name] = f;
      return f;
    }
    var scaleK = 1;

    function meta(name) {
      var m = sheets && sheets[name];
      if (!m || typeof m !== 'object') return null;
      var f = m.frames | 0, w = +m.w, h = +m.h, fps = +m.fps;
      if (!(f > 0) || !(w > 0) || !(h > 0)) return null;
      // A long clip is a GRID, not one row: WebP refuses a side over 16383 px.
      // `cols` missing = the historical single-row strip.
      var cols = m.cols | 0;
      if (!(cols > 0) || cols > f) cols = f;
      var cx = +m.cx;
      if (!(cx > 0) || !(cx < 1)) cx = 0.5;
      var by = +m.by;
      if (!(by > 0) || !(by <= 1)) by = 1;
      var byMax = +m.by_max;
      if (!(byMax > 0) || !(byMax <= 1)) byMax = by;
      var ey = +m.ey;
      if (!(ey > 0) || !(ey < 1)) ey = 0.43;
      var gz = Array.isArray(m.gaze) && m.gaze.length === f ? m.gaze : null;
      return { frames: f, w: w, h: h, cols: cols, rows: Math.ceil(f / cols),
               cx: cx, by: by, by_max: byMax, ey: ey, gaze: gz, align: m.align || 'start',
               by_from: +m.by_from, by_to: +m.by_to,
               fps: (fps > 0 ? fps : 12), segments: m.segments || null };
    }
    (function () {
      // Every sheet gets its element now, so the browser has them loaded and
      // decoded long before a clip switch asks for one.
      var seen = {};
      for (var k in sheets) {
        if (!Object.prototype.hasOwnProperty.call(sheets, k) || !meta(k)) continue;
        var file = (sheets[k] && sheets[k].file) || k;
        if (seen[file]) continue;
        seen[file] = 1;
        filmFor(k);
      }
    })();
    (function () {
      // The resting pose is the reference for BOTH: the axis it is mirrored
      // about and the line its feet stand on.
      var order = ['idle', 'head', 'sleep', 'run', 'run_in'];
      for (var i = 0; i < order.length; i++) {
        var m = meta(order[i]);
        if (m) { cxRef = m.cx; byRef = m.by; return; }
      }
    })();
    // The strip is an IMAGE inside a clipping box, moved with a transform —
    // not a background whose position changes. A background-position change
    // repaints the element, and repainting a 3400 px sheet behind a drop
    // shadow is work the browser cannot always finish in a frame: the picture
    // drops out and the page shows through (owner, stand 2026-09-12: "white
    // holes all the time"). A transform is composited, so the frames cost nothing
    // to advance and the sheet is rasterised once.
    function applySheet(name) {
      var m = meta(name);
      if (!m) return false;
      var f = filmFor(name);
      // Only ever show a sheet the browser has finished with. If it is not
      // ready yet the current clip keeps playing and the caller tries again on
      // the next frame — a late sheet costs a moment of the old animation, not
      // a hole in the page.
      if (!f || !f.complete || !f.naturalWidth) return false;
      var box = boxOf() || m.w;
      scaleK = box / m.w;
      f.style.width = (m.cols * m.w * scaleK).toFixed(2) + 'px';
      f.style.height = (m.rows * m.h * scaleK).toFixed(2) + 'px';
      if (film && film !== f) { film.style.visibility = 'hidden'; film.classList.remove('is-on'); }
      f.style.visibility = 'visible';
      f.classList.add('is-on');
      film = f;
      cur = name;
      return true;
    }
    function posOf(m, f) {
      var col = f % m.cols, row = (f / m.cols) | 0;
      return 'translate3d(' + (-(col * m.w * scaleK)).toFixed(2) + 'px,' +
             (-(row * m.h * scaleK)).toFixed(2) + 'px,0)';
    }
    // Out of the layer tree entirely when it is not blending: an opacity-0
    // layer over a 3840 px sheet still costs a raster on every repaint.

    function paint() {
      var m = meta(cur);
      if (!m || !film) return;
      film.style.transform = posOf(m, frame);
    }
    function loopRaf(ts) {
      if (dead) return;
      raf = requestAnimationFrame(loopRaf);
      if (!last) last = ts;
      var dt = ts - last;
      last = ts;
      if (dt < 0 || dt > 250) dt = 16;
      if (waiting) {
        waiting.ms += dt;
        if (applySheet(waiting.name)) {
          var w = waiting; waiting = null;
          startClip(w.name, w.opts);
        } else if (waiting.ms > 4000) {
          // Four seconds is not a slow network, it is a broken sheet.
          var w2 = waiting; waiting = null;
          if (w2.opts && w2.opts.onEnd) { try { w2.opts.onEnd(); } catch (e3) {} }
        }
      }
      if (tickCb) { try { tickCb(dt); } catch (e) {} }
      if (!playing || !cur) return;
      var m = meta(cur);
      if (!m) return;
      var spf = 1000 / m.fps;
      fpsAcc += dt;
      var moved = false;
      while (fpsAcc >= spf) {
        fpsAcc -= spf;
        var atEnd = dir > 0 ? frame >= to : frame <= from;
        if (atEnd) {
          // Ping-pong: run the motion out and back, so the clip finishes in
          // the pose it started from and can hand over without a pop.
          if (pingpong && dir > 0) { dir = -1; frame = Math.max(from, frame - 1); moved = true; }
          else if (looping) { dir = 1; frame = from; moved = true; }
          else {
            playing = false; fpsAcc = 0;
            var cb = ended; ended = null;
            paint();
            if (cb) { try { cb(); } catch (e2) {} }
            return;
          }
        } else { frame += dir; moved = true; }
      }
      if (moved) paint();
    }
    function startClip(name, opts) {
      var m = meta(name);
      if (!m) return;
      from = Math.max(0, opts.from | 0);
      to = (opts.to === undefined || opts.to === null) ? m.frames - 1 : Math.min(m.frames - 1, opts.to | 0);
      if (to < from) to = from;
      // `at` picks up a loop where it left off, so a page change does not
      // restart the creature's breathing.
      frame = from;
      if (opts.at !== undefined && opts.at !== null) {
        var a = Math.round(+opts.at);
        if (isFinite(a)) frame = Math.min(to, Math.max(from, a));
      }
      fpsAcc = 0; looping = !!opts.loop; ended = opts.onEnd || null; playing = true;
      pingpong = !!opts.pingpong; dir = 1;
      paint(); ensure();
    }
    function ensure() { if (!dead && !raf) { last = 0; raf = requestAnimationFrame(loopRaf); } }

    return {
      kind: 'sprite',
      has: function (name) { return !!meta(name); },
      meta: meta,
      clip: function () { return cur; },
      cx: function () { var m = meta(cur); return m && isFinite(m.cx) ? m.cx : 0.5; },
      cxRef: function () { return cxRef; },
      // Debug transport: pause keeps from/to/looping/onEnd, so resume carries
      // on exactly where it stopped.
      // Restart the animation loop if it stopped — a tab that was hidden for a
      // while can leave it without a pending frame.
      kick: function () { ensure(); },
      pause: function () { playing = false; },
      resume: function () { if (cur) { playing = true; ensure(); } },
      paused: function () { return !playing; },
      // One entry per SHEET: run_in / run_out / walk_out are the same file
      // under three names, and three identical rows in a picker help nobody.
      names: function () {
        var out = [], seen = {};
        for (var k in sheets) {
          if (!Object.prototype.hasOwnProperty.call(sheets, k) || !meta(k)) continue;
          var f = (sheets[k] && sheets[k].file) || k;
          if (seen[f]) continue;
          seen[f] = 1;
          out.push(k);
        }
        return out.sort();
      },
      count: function (name) { var m = meta(name || cur); return m ? m.frames : 0; },
      byRef: function () { return byRef; },
      frame: function () { return frame; },
      duration: function (name) { var m = meta(name); return m ? Math.round(m.frames / m.fps * 1000) : 0; },
      onTick: function (cb) { tickCb = cb; ensure(); },
      play: function (name, opts) {
        opts = opts || {};
        var m = meta(name);
        if (!m) return false;
        if (cur !== name && !applySheet(name)) {
          // The sheet is not ready yet. Hold the clip that IS on screen and
          // start this one the moment it can be shown, rather than cutting to
          // nothing.
          waiting = { name: name, opts: opts, ms: 0 };
          ensure();
          return true;
        }
        startClip(name, opts);
        return true;
      },
      // Sub-frame seeking without blending. Two poses cross-faded are two
      // half-transparent copies of the creature: you see the page through it
      // and it reads as flicker (owner, stand 2026-09-12: "it blinks
      // constantly"). The arc has enough frames to step through honestly.
      seekF: function (name, f) {
        return this.seek(name, Math.round(f));
      },
      seek: function (name, f) {
        var m = meta(name);
        if (!m) return false;
        if (cur !== name && !applySheet(name)) return false;
        playing = false; ended = null;
        f = Math.round(+f);
        if (!isFinite(f)) f = 0;
        frame = Math.max(0, Math.min(m.frames - 1, f));
        paint(); ensure();
        return true;
      },
      stop: function () { playing = false; ended = null; },
      destroy: function () { dead = true; playing = false; ended = null; tickCb = null; if (raf) cancelAnimationFrame(raf); raf = 0; }
    };
  }

  // Fallback with the original single PNG poses: same API, no frames. Used
  // when sheets.json is unreachable/malformed or a clip is missing.
  var POSE_OF = { run_in: 'run', run_out: 'run', idle: 'sit', head: 'look', stretch: 'stretch', sleep: 'sleep', peek: 'peek' };
  function createImgPlayer(node) {
    var cur = null, dead = false, tid = 0, tickCb = null, raf = 0;
    function set(name) {
      var pose = POSE_OF[name] || 'sit';
      if (cur !== name) { node.src = img(pose); cur = name; }
    }
    function loopRaf(ts) {
      if (dead) return;
      raf = requestAnimationFrame(loopRaf);
      if (tickCb) { try { tickCb(16); } catch (e) {} }
      return ts;
    }
    return {
      kind: 'img',
      has: function (name) { return !!POSE_OF[name]; },
      meta: function () { return null; },
      clip: function () { return cur; },
      cxRef: function () { return 0.5; },
      kick: function () {},
      pause: function () {},
      resume: function () {},
      paused: function () { return false; },
      names: function () { var o = []; for (var k in POSE_OF) o.push(k); return o; },
      count: function () { return 1; },
      byRef: function () { return 1; },
      frame: function () { return 0; },
      duration: function (name) { return name === 'run_in' || name === 'run_out' ? 1100 : 900; },
      onTick: function (cb) { tickCb = cb; if (!dead && !raf) raf = requestAnimationFrame(loopRaf); },
      play: function (name, opts) {
        opts = opts || {};
        set(name);
        if (tid) { clearTimeout(tid); tid = 0; }
        if (!opts.loop && opts.onEnd) {
          tid = setTimeout(function () { tid = 0; if (!dead) opts.onEnd(); }, this.duration(name));
        }
        return true;
      },
      seek: function (name) { set(name); return true; },
      seekF: function (name) { set(name); return true; },
      stop: function () { if (tid) { clearTimeout(tid); tid = 0; } },
      destroy: function () { dead = true; tickCb = null; if (tid) clearTimeout(tid); tid = 0; if (raf) cancelAnimationFrame(raf); raf = 0; }
    };
  }

  // ---- one fetch for the manifest, only now (owner, gated page, planned) --
  var SHEETS = null, SHEETS_READY = false;
  function loadSheets() {
    if (typeof fetch !== 'function') return;
    fetch(BASE + 'sheets/sheets.json?v=' + ASSET_V, { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('http'); return r.json(); })
      .then(function (j) { if (j && typeof j === 'object' && !Array.isArray(j)) SHEETS = j; })
      .catch(function () { SHEETS = null; })
      .then(function () {
        // Everything the entrance needs, DECODED, before the entrance. Just
        // fetching is not enough: the sheet is decoded when the browser first
        // paints it, and a clip switch onto an undecoded sheet paints nothing
        // at all — the creature vanishes and the page shows through it (owner,
        // stand 2026-09-12: "it runs white… when the landing video ended
        // also a white bang").
        if (!SHEETS) return;
        var want = ['run_in', 'sitdown', 'idle', 'head'];
        var left = 0;
        want.forEach(function (c) {
          if (!SHEETS[c]) return;
          left++;
          var i = new Image();
          i.src = sheetUrl(c);
          var done = function () { left--; if (left <= 0) SHEETS_READY = true; };
          if (typeof i.decode === 'function') i.decode().then(done, done);
          else { i.onload = done; i.onerror = done; }
        });
        if (!left) SHEETS_READY = true;
        // The rest follow without holding anything up: they are only needed
        // minutes later, but they must not be decoded mid-switch either.
        ['pet', 'stretch', 'sleep', 'standup', 'run_out'].forEach(function (c) {
          if (!SHEETS[c]) return;
          var j = new Image();
          j.src = sheetUrl(c);
          if (typeof j.decode === 'function') j.decode().then(function () {}, function () {});
        });
      });
  }

  // A readout under the creature, for reporting exactly WHEN something went
  // wrong on a machine that is not this one. Off unless asked for: `?fox=1`
  // turns it on for good on this browser, `?fox=0` turns it off again.
  var DEBUG = false;
  try {
    var q = String(location.search || '');
    if (q.indexOf('fox=1') >= 0) localStorage.setItem('op-companion-debug', '1');
    else if (q.indexOf('fox=0') >= 0) localStorage.removeItem('op-companion-debug');
    DEBUG = localStorage.getItem('op-companion-debug') === '1';
  } catch (e) {}
  var T0 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
  var readout = null, lastRead = '', worstDt = 0, worstAt = 0;
  var PAUSED = false, panel = null, scrub = null, picker = null, lastScrub = -1;
  // The entrance travel, driven here rather than by a CSS curve. A gait moves
  // at a CONSTANT speed, and a CSS ease spent most of the distance in the
  // first half second and then crawled — the legs kept running while the body
  // had all but stopped (owner, stand 2026-09-12: "between run-in and sit down
  // there is a gap in motion"). So: constant speed while the run clip plays, then a
  // linear deceleration to a stop that starts at exactly that speed and ends
  // as the paws plant.
  var travel = null;
  function nowMs() {
    return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
  }
  function startTravel(from, to, runMs) {
    var v = Math.abs(from - to) / Math.max(1, runMs);      // px per ms
    travel = { t0: nowMs(), runMs: runMs, from: from, to: to,
               decelMs: v > 0 ? Math.round(2 * Math.abs(to) / v) : 0 };
    updateTravel();
  }
  function updateTravel() {
    if (!travel || !el) return;
    var t = nowMs() - travel.t0;
    var x;
    if (t <= travel.runMs) {
      x = travel.from + (travel.to - travel.from) * (t / travel.runMs);
    } else if (travel.decelMs > 0) {
      var u = Math.min(1, (t - travel.runMs) / travel.decelMs);
      x = travel.to * (1 - (2 * u - u * u));               // v -> 0, linearly
      if (u >= 1) { travel = null; }
    } else {
      x = 0; travel = null;
    }
    el.style.transform = 'translateX(' + x.toFixed(1) + 'px)';
  }
  function updateReadout(dt) {
    if (!readout || !player) return;
    var now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    // The worst animation frame of the last two seconds: if the picture drops
    // out exactly when this spikes, the browser is failing to draw in time.
    if (dt > worstDt || now - worstAt > 2000) { worstDt = dt; worstAt = now; }
    var clip = player.clip() || '-';
    var where = '';
    if (clip === 'head' && headSegs) {
      var f0 = player.frame(), rr = segEnds(headSegs.right);
      where = (rr && f0 >= Math.min(rr[0], rr[1])) ? ' right' : ' arc';
    }
    var txt = ((now - T0) / 1000).toFixed(1) + 's  ' + clip + where +
              ' ' + player.frame() + '/' + Math.max(0, player.count() - 1) +
              '  ' + Math.round(worstDt) + 'ms';
    if (txt === lastRead) return;
    lastRead = txt;
    readout.textContent = txt;
    if (scrub && !PAUSED) {
      var max = Math.max(0, player.count() - 1);
      if (+scrub.max !== max) scrub.max = String(max);
      if (player.frame() !== lastScrub) { lastScrub = player.frame(); scrub.value = String(lastScrub); }
    }
  }

  // ---- debug transport (only with ?fox=1) ---------------------------------
  function dbgButton(label, title, fn) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.title = title;
    b.addEventListener('click', function (e) { e.stopPropagation(); e.preventDefault(); fn(b); });
    return b;
  }
  function setPaused(on) {
    PAUSED = !!on;
    if (!player) return;
    if (PAUSED) player.pause(); else player.resume();
  }
  // The arrival is three clips in a row, so stepping walks them as ONE
  // sequence: past the last frame of the run comes the first frame of the
  // landing, and so on (owner, stand 2026-09-12: "I can't step through the
  // frames in sequence, they are not grouped in any order").
  var DBG_SEQ = ['run_in', 'sitdown', 'idle', 'standup', 'run_out'];
  function stepFrame(d) {
    if (!player) return;
    var c = player.clip();
    if (!c) return;
    setPaused(true);
    var max = Math.max(0, player.count() - 1);
    var f = player.frame() + d;
    if (f > max || f < 0) {
      var i = DBG_SEQ.indexOf(c === 'run' ? 'run_in' : c);
      var j = i < 0 ? -1 : i + (f > max ? 1 : -1);
      if (j >= 0 && j < DBG_SEQ.length && player.has(DBG_SEQ[j])) {
        c = DBG_SEQ[j];
        player.play(c, {});
        player.pause();
        max = Math.max(0, player.count(c) - 1);
        f = (d > 0) ? 0 : max;
      } else {
        f = Math.min(max, Math.max(0, f));
      }
    }
    player.seek(c, f);
    player.pause();
    if (scrub) { scrub.max = String(max); scrub.value = String(f); lastScrub = f; }
    if (picker && picker.value !== c) picker.value = c;
    lastRead = '';
  }
  function buildPanel() {
    panel = document.createElement('div');
    panel.className = 'op-comp-dbg';
    panel.addEventListener('click', function (e) { e.stopPropagation(); });
    panel.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
    var play = dbgButton('||', 'pause / resume', function (b) {
      setPaused(!PAUSED);
      b.textContent = PAUSED ? '>' : '||';
    });
    panel.appendChild(dbgButton('|<', 'previous frame', function () { stepFrame(-1); }));
    panel.appendChild(play);
    panel.appendChild(dbgButton('>|', 'next frame', function () { stepFrame(1); }));
    scrub = document.createElement('input');
    scrub.type = 'range';
    scrub.min = '0';
    scrub.max = String(Math.max(0, player.count() - 1));
    scrub.value = '0';
    scrub.addEventListener('input', function (e) {
      e.stopPropagation();
      setPaused(true);
      var c = player.clip();
      if (c) { player.seek(c, +scrub.value); lastScrub = +scrub.value; lastRead = ''; }
    });
    scrub.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
    panel.appendChild(scrub);
    picker = document.createElement('select');
    player.names().forEach(function (nm) {
      var o = document.createElement('option');
      o.value = nm; o.textContent = nm;
      picker.appendChild(o);
    });
    picker.value = player.clip() || 'idle';
    picker.addEventListener('change', function (e) {
      e.stopPropagation();
      mode = 'busy';
      setPaused(false);
      player.play(picker.value, { loop: true });
      setPaused(true);
      if (scrub) { scrub.max = String(Math.max(0, player.count() - 1)); scrub.value = '0'; lastScrub = 0; }
      lastRead = '';
    });
    picker.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
    panel.appendChild(picker);
    panel.appendChild(dbgButton('<<', 'replay the entrance', function () {
      if (!el) return;
      setPaused(false);
      mode = 'enter';
      el.style.transition = 'none';
      el.style.transform = 'translateX(520px)';
      requestAnimationFrame(function () {
        if (!el) return;
        var d2 = player.duration('run_in') || 1100;
        el.style.transition = 'opacity .35s ease';
        startTravel(520, 70, d2);
        player.play('run_in', { onEnd: arrive });
      });
    }));
    el.appendChild(panel);
  }

  var el = null, sprite = null, player = null, timers = [], gone = false, lastInput = Date.now();
  var mode = 'off';            // off|enter|idle|sweep|track|return|busy|sleep|out
  var headMeta = null, headSegs = null, headCentre = 0, headSweep = null, headGaze = null;
  // px: at this distance the head is turned as far as the art goes. The
  // creature and the pointer live on the SAME surface, so a pointer close by
  // but off to the side is a WIDE angle, not a small one: at 560 px a cursor
  // moving right next to the fox barely moved its head at all (owner's screen
  // recording, 2026-09-11: "up close the eyes do not follow the mouse at all").
  var GAZE_REACH = 230;
  // 'left' | 'right' | null — the one side the head strip covers; the other
  // side is that side mirrored.
  var mirrorSide = null, flipped = false;
  // 'right' | 'left' — the direction the run strip is drawn facing.
  var runFaces = 'right';
  // Mirror the sprite when the travel direction differs from the drawing.
  function faceTravel(node, dir) {
    node.classList.toggle('is-mirror', dir !== runFaces);
    applyFacing();
  }
  function setFlip(on) {
    on = !!on;
    if (on === flipped || !el) return;
    flipped = on;
    el.classList.toggle('is-flip', on);
    applyFacing();
  }
  // Mirroring flips about the CELL's centre, and the creature does not sit in
  // the middle of its cell — so a bare scaleX(-1) shifts it sideways and it
  // looks like the body teleports when the head turns (owner, stand
  // 2026-09-11). The manifest carries `cx`, the character's own centre as a
  // fraction of the cell, and the flip is compensated by 2*cx-1 of the box.
  var lastFacing = '', turning = false;
  // scaleX(s) about the box centre keeps the body axis put when the box is
  // shifted by (pivot - W/2)*(1 - s); s = -1 is the mirror, s ~ 0 the turn.
  function faceCss(s, node) {
    node = node || sprite;
    var w = (node && node.clientWidth) || (el && el.clientWidth) || 0;
    var cx = (player && player.cxRef) ? player.cxRef() : 0.5;
    var t = (cx * w - w / 2) * (1 - s);
    return 'translateX(' + t.toFixed(2) + 'px) scaleX(' + s + ')';
  }
  function mirrorOn() {
    return !!el && el.classList.contains('is-mirror') !== el.classList.contains('is-flip');
  }
  // Every clip is framed differently — a fox at full stride fills more of the
  // cell than one sitting — so each one is shifted vertically to stand on the
  // SAME line. Without it the running fox sank through the bottom of the
  // window and its paws were cut off (owner, stand 2026-09-12: "a hole while running
  // and sitting down").
  // How far this clip has to be shifted to stand on the reference line, in px.
  function groundOffset(pl, h) {
    if (!pl || !pl.byRef || !pl.meta) return 0;
    var m = pl.meta(pl.clip());
    if (!m || !isFinite(m.by)) return 0;
    var by;
    if (m.align === 'floor') {
      by = m.by_max;
    } else if (m.align === 'ramp' && isFinite(m.by_from) && isFinite(m.by_to)) {
      var nn = Math.max(1, m.frames - 1);
      var t = Math.min(1, Math.max(0, pl.frame() / nn));
      by = m.by_from + (m.by_to - m.by_from) * t;
    } else {
      by = m.by;
    }
    var d = (pl.byRef() - by) * h;
    return isFinite(d) ? d : 0;
  }
  function groundCss() {
    if (!player || !player.byRef || !player.meta) return '';
    var m = player.meta(player.clip());
    if (!m || !isFinite(m.by)) return '';
    // `align` says which line of the clip is its ground: the pose it starts
    // from, the floor it runs on, or a ramp between the two for a clip that
    // bridges two shoots. See groundOffset().
    var h = (sprite && sprite.clientHeight) || (el && el.clientHeight) || 0;
    var dy = groundOffset(player, h);
    if (Math.abs(dy) < 0.4) return '';
    return 'translateY(' + dy.toFixed(2) + 'px)';
  }
  // The head clip is a still body with a moving head, so while it plays
  // NOTHING about the creature moves once the gaze settles — parked at the far
  // left it read as a statue (owner, 2026-09-12: "the head barely
  // moves any more", "I want to bring it to life a little"). A breath is the honest
  // thing one sprite can do: the whole body, about a pixel, around the feet.
  // A TRANSLATE only, never a scale. Scaling this element means re-rasterising
  // the sheet it holds on every frame, and that is what made the head follow
  // the pointer in fits (owner's screen recording, 2026-09-12: "the head stopped
  // following the mouse smoothly"). A translate is composited and free.
  var BREATH_MS = 3400, BREATH_PX = 1.6;
  function breathCss() {
    if (!player || player.clip() !== 'head') return '';
    var t = ((typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now());
    var a = Math.sin((t % BREATH_MS) / BREATH_MS * Math.PI * 2);
    return 'translateY(' + (a * BREATH_PX).toFixed(2) + 'px)';
  }
  function applyFacing() {
    if (!el || !sprite || turning) return;
    var css = groundCss();
    var br = breathCss();
    if (br) css = (css ? css + ' ' : '') + br;
    if (mirrorOn()) css = (css ? css + ' ' : '') + faceCss(-1);
    if (css === lastFacing) return;
    lastFacing = css;
    sprite.style.transition = '';
    sprite.style.transform = css;
  }
  var nearAt = 0;
  // Stroking: the pointer moving ACROSS the creature, not a click. Distance is
  // accumulated while the pointer is over it and decays when it is not, so a
  // hand resting there does nothing and a hand moving over it is felt.
  var STROKE_PX = 140;         // how much movement over it counts as a stroke
  var STROKE_WINDOW_MS = 1500; // within this long
  var STROKE_COOLDOWN_MS = 7000;
  // How long it keeps watching a pointer that has stopped moving. Long enough
  // to feel attentive, short enough that it goes back to the idle loop — where
  // it breathes, blinks and sways its tail — instead of holding a pose.
  var HOLD_GAZE_MS = 5200;
  var lastMoveAt = 0;
  var strokeAcc = 0, strokeAt = 0, strokeLast = null, pettedAt = 0;
  // The eased gaze itself: x in -1..1 (left..right), y in 0..1 (up).
  var aimTarget = null;        // the frame the pointer asks for
  var headShown = null;        // the frame actually on screen, rate-limited
  var ptr = null;              // last pointer position, screen coords
  var mo = null;

  function later(fn, ms) { var id = setTimeout(fn, ms); timers.push(id); return id; }
  function clearTimers() { timers.forEach(clearTimeout); timers = []; }

  function overlayOpen() {
    // `#op-season-intro` — the full-screen season opening story. Without it in
    // this list the fox came out UNDER the video and ran away exactly when
    // the person closed it: a click on "Skip" is, for her, an ordinary
    // click elsewhere (owner, 05.10: "why does our fox run away at the end of the
    // video"). Story first, then the fox — she waits her turn,
    // `mount` re-checks after 15 seconds.
    return !!document.querySelector(
      '.overlay.visible, [data-palette-open="1"], #cmdk.open, .modal.open, #op-season-intro.is-on');
  }

  var waitedForSheets = 0;
  function mount() {
    if (gone || el) return;
    if (overlayOpen()) { later(mount, 15000); return; }
    // The first delay is short now, so the manifest — and the sheets it names
    // — can still be in flight. Give them a moment rather than falling back to
    // the flat PNG poses or, worse, starting the entrance on a sheet that has
    // not been decoded yet.
    if ((!SHEETS || !SHEETS_READY) && waitedForSheets < 24) {
      waitedForSheets++; later(mount, 150); return;
    }
    // Re-read the budget NOW: the plan is the same in every tab of this
    // person, so two tabs opened together would otherwise both show.
    var c = readCounters();
    if (document.getElementById('op-companion')) { gone = true; return; }
    if (!scene.resident && (c.shownThisTab || (c.day === today && c.n >= PER_DAY))) { gone = true; return; }
    el = document.createElement('div');
    el.id = 'op-companion';
    el.setAttribute('data-pos', scene.pose === 'peek' ? 'edge' : 'corner');
    if (firstDay) el.setAttribute('data-glow', '1');
    el.setAttribute('aria-hidden', 'true');
    // The class is not decoration: CSS uses it to tell the still variant from the
    // moving one and not hide it under "reduce motion".
    if (STILL) el.classList.add('is-still');
    var glow = document.createElement('i'); glow.className = 'op-comp-glow'; el.appendChild(glow);
    var dust = document.createElement('i'); dust.className = 'op-comp-dust'; el.appendChild(dust);

    var useSheets = !!(SHEETS && SHEETS.idle);
    if (useSheets) {
      sprite = document.createElement('div');
      sprite.className = 'op-comp-sprite';
      el.appendChild(sprite);
      document.body.appendChild(el);
      // Measure the sprite node itself, not the module's `el`: leave() nulls
      // `el` before the exit clip plays, and a stale fallback made the fox
      // shrink to half size exactly as it ran away.
      var spriteNode = sprite;
      player = createSpritePlayer(sprite, SHEETS, function () {
        return spriteNode.clientWidth || (spriteNode.parentNode && spriteNode.parentNode.clientWidth) || 235;
      });
      if (!player.has('idle')) { player.destroy(); player = null; el.removeChild(sprite); sprite = null; useSheets = false; }
    } else {
      document.body.appendChild(el);
    }
    if (!useSheets) {
      sprite = document.createElement('img');
      sprite.alt = ''; sprite.draggable = false;
      sprite.src = img(scene.pose === 'peek' ? 'peek' : 'run');
      el.appendChild(sprite);
      player = createImgPlayer(sprite);
    }

    // Which way the run strip's fox is drawn. Verified per strip and written
    // into the manifest — assuming it was the cause of "it runs backwards":
    // every travel was mirrored the wrong way round.
    var runMeta = (SHEETS && (SHEETS.run_in || SHEETS.run)) || null;
    runFaces = (runMeta && runMeta.faces === 'left') ? 'left' : 'right';
    headMeta = player.meta && player.meta('head');
    headSegs = headMeta && headMeta.segments ? headMeta.segments : null;
    if (headSegs) {
      headCentre = isFinite(+headSegs.center) ? +headSegs.center : 0;
      headGaze = headMeta.gaze || null;
      // The strip covers both sides and up since 2026-09-11, so NOTHING is
      // ever mirrored: a mirrored body swaps the tail from side to side and
      // reads as the creature changing pose (owner: "let it sit in one
      // position"). The idle glance is a gaze sweep, not a range of frames.
      headSweep = isFinite(+headSegs.arc_left) && isFinite(+headSegs.arc_up);
      mirrorSide = null;
      aimTarget = null; headShown = null;
    }

    if (!scene.resident) noteShown(today);
    if (scene.resident) {
      noteHere();
      later(keepHere, 2000);
      // The exact frame is the one it is on as the page goes away; the timer
      // above is only a fallback for a tab that dies without warning.
      window.addEventListener('pagehide', noteHere);
      window.addEventListener('beforeunload', noteHere);
    }
    if (firstDay) {
      try {
        var fk = 'op-companion-firstday-shown-' + UID;
        localStorage.setItem(fk, String((parseInt(localStorage.getItem(fk) || '0', 10) || 0) + 1));
      } catch (e) {}
    }

    // Entrance. A corner fox RUNS in: the run_in strip plays while the
    // element eases from off-screen left to its corner over exactly the
    // clip's duration. A peeking fox has no run — it just slides out of the
    // right edge and settles into idle.
    var staying = scene.pose === 'stay';
    var runIn = !staying && scene.pose !== 'peek' && player.has('run_in');
    var dur = runIn ? (player.duration('run_in') || 1100) : 800;
    // Far enough that the gait reads as a run: ~2 s over 520 px ≈ 260 px/s.
    // The run does NOT cover the whole distance: the last 70 px are travelled
    // while the landing clip plays, because the fox is still striding at the
    // start of it. Stopping the travel dead on the run's last frame left it
    // pedalling on the spot (owner, stand 2026-09-12: "between run-in and sit down
    // there is a gap in motion").
    el.style.transform = staying ? 'translateX(0)'
      : (scene.pose === 'peek' ? 'translateX(60px)' : 'translateX(520px)');
    el.style.transition = (runIn || staying)
      ? 'opacity .35s ease'
      : 'transform ' + dur + 'ms cubic-bezier(.2,.8,.2,1), opacity .35s ease';
    mode = 'enter';
    if (DEBUG) {
      readout = document.createElement('div');
      readout.className = 'op-comp-readout';
      el.appendChild(readout);
      buildPanel();
    }
    // Where the pointer was on the page before this one. Without it the
    // creature faces front until the mouse MOVES, and a person clicking
    // through the sidebar never moves it — so it looked straight ahead the
    // whole way (owner's screen recording, 2026-09-12).
    if (!ptr && herePtr) ptr = { x: herePtr.x, y: herePtr.y };
    player.onTick(tick);
    requestAnimationFrame(function () { requestAnimationFrame(function () {
      if (!el || gone) return;
      if (staying) el.classList.add('is-instant');
      el.classList.add('is-in');
      if (runIn) {
        noteEntered();
        startTravel(520, 70, dur);
        faceTravel(el, 'left');           // in from the right edge, leftwards
        // The wall clock backs up the clip's own end, because a hidden tab
        // gives no animation frames and the run would never finish — the
        // creature would be left standing mid-stride until the person came
        // back (owner, stand 2026-09-12).
        var landed = false;
        var land = function () { if (!landed) { landed = true; arrive(); } };
        player.play('run_in', { onEnd: land });
        later(land, dur + 250);
        later(function () { if (el) el.classList.add('is-skid'); }, Math.max(0, dur - 420));
      } else if (STILL) {
        // Not a single frame per second: the person asked the system for no
        // motion, and "quiet breathing" is motion too. One frame and that's it.
        el.style.transform = 'translateX(0)';
        player.seek('idle', 0);
        mode = 'idle';
      } else if (staying) {
        // Already here: the idle loop picked up where it was, in the corner,
        // with no fade — a page change should not look like an arrival at all
        // (owner, 2026-09-12: "so that when switching tabs the fox does not
        // flicker after all").
        el.style.transform = 'translateX(0)';
        player.play('idle', { loop: true, at: hereFrame });
        mode = 'idle';
        scheduleSweep();
        later(checkSleep, 5000);
      } else {
        el.style.transform = 'translateX(0)';
        player.play('idle', { loop: true });
        later(arrive, dur);
      }
    }); });

    if (scene.staySec > 0) later(function () { if (!PAUSED) leave('walk'); }, scene.staySec * 1000);
    if (!STILL) later(checkSleep, 5000);
    // Each of these handlers exists for the sake of motion: stretching in
    // response to a click, turning the head after the cursor, running off on Ctrl+K. With
    // "reduce motion" none of them is attached — the fox just sits.
    if (!STILL) {
    el.addEventListener('click', onSelfClick);
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('click', onDocClick, true);
    document.addEventListener('visibilitychange', onVisible);
    document.addEventListener('pointermove', onInput, { passive: true });
    document.addEventListener('pointerdown', onInput, { passive: true });
    document.addEventListener('pointerover', onInput, { passive: true });
    document.addEventListener('wheel', onInput, { passive: true });
    }
    document.addEventListener('keydown', onInput, { passive: true });
    if (typeof MutationObserver !== 'undefined') {
      mo = new MutationObserver(function () { if (overlayOpen()) leave('run'); });
      mo.observe(document.body, { attributes: true, attributeFilter: ['class'], subtree: true });
    }
  }

  // Landed in the corner: the dust settles and the idle loop takes over.
  function arrive() {
    if (!el || gone) return;
    later(function () { if (el) el.classList.remove('is-skid'); }, 800);
    // The travel carries on into the landing clip and coasts to a stop there;
    // `updateTravel` owns it until then.
    // The run does not hand straight over to the sitting pose any more — a fox
    // at full stride and a fox sitting still are not the same picture, and the
    // cut between them showed (owner, stand 2026-09-12: "the turn after running
    // is awful"). `sitdown` is a clip of exactly that movement: it slows,
    // plants its paws, turns to the room and sits, ending in the pose `idle`
    // starts from.
    if (player.has('sitdown')) {
      var sm = player.meta ? player.meta('sitdown') : null;
      var act = sm && sm.segments && segEnds(sm.segments.action);
      var opts = { onEnd: function () { if (el && !gone) toIdle(); } };
      if (act) { opts.from = Math.min(act[0], act[1]); opts.to = Math.max(act[0], act[1]); }
      // The ground ramp is written against the whole clip, so it must play
      // whole: a trimmed range would stop the creature part-way down.
      mode = 'busy';
      player.play('sitdown', opts);
    } else {
      toIdle();
    }
    scheduleSweep();
  }
  function toIdle() {
    if (!el || gone) return;
    mode = 'idle';
    player.play('idle', { loop: true });
  }
  // Every 6–10 s, when the pointer is nowhere near, a brief look left and
  // right: the head strip played once across its left..right span.
  function scheduleSweep() {
    later(function () {
      if (!el || gone) return;
      scheduleSweep();
      if (mode !== 'idle' || !player.has('head')) return;
      mode = 'sweep';
      player.stop();
      // A glance around, driven along the very same arc as the pointer
      // tracking, so it turns exactly as smoothly: level left, up, back.
      // The glance uses the same measured gaze: a look to the side, a look up,
      // and back to the front.
      var lf, up;
      if (headGaze) {
        lf = frameForVec(headGaze, -1, 0, 0.8);
        up = frameForVec(headGaze, 0, -1, 0.7);
      } else {
        lf = +headSegs.arc_left; up = +headSegs.arc_up;
      }
      var seq = [[isFinite(lf) ? lf : headCentre, 1100],
                 [isFinite(up) ? up : headCentre, 900],
                 [headCentre, 700]];
      var i = 0;
      (function step() {
        if (!el || gone || mode !== 'sweep') return;
        if (i >= seq.length) { toIdle(); return; }
        aimTarget = seq[i][0];
        var ms = seq[i][1];
        i++;
        later(step, ms);
      })();
    }, 6000 + Math.round(Math.random() * 4000));
  }

  // The animation tick: everything that follows the pointer happens here, at
  // the arc's own frames, never faster than HEAD_MAX_FPS, so the head TURNS
  // rather than snaps.
  function tick(dt) {
    if (!el || gone || mode === 'out') return;
    if (travel) updateTravel();
    if (readout) updateReadout(dt);
    if (PAUSED) return;
    applyFacing();   // cheap: a cached string, written only when it changes
    if (mode === 'enter') return;
    if (!headSegs || !headMeta) return;
    var now = Date.now();
    if (ptr && mode !== 'busy' && mode !== 'sleep') {
      var r = el.getBoundingClientRect();
      // The eyes, where the pipeline actually FOUND them in this clip's art —
      // the row with the most dark pixels in the upper body — plus whatever
      // the ground alignment has shifted the clip by. A guessed fraction of
      // the box put the origin some 20 px above the real eyes, which bends
      // every angle (colleague's review, 2026-09-12).
      var hm = player.meta ? player.meta(player.clip()) : null;
      var eyeFrac = (hm && isFinite(hm.ey)) ? hm.ey : 0.43;
      var ex = r.left + r.width * (player.cxRef ? player.cxRef() : 0.5);
      var ey = r.top + r.height * eyeFrac + groundOffset(player, r.height);
      var dx = ptr.x - ex, dy = ptr.y - ey;
      if (Math.sqrt(dx * dx + dy * dy) < trackRadius()) {
        nearAt = now;
        if (mode !== 'track') {
          player.stop();
          if (player.clip() !== 'head') headShown = null;
          mode = 'track';
        }
        if (headGaze) {
          // Pick the frame that looks nearest the pointer, from the measured
          // gaze of every frame — no hand-marked «level» or «up».
          var L = Math.sqrt(dx * dx + dy * dy) || 1;
          aimTarget = frameForVec(headGaze, dx / L, dy / L, L / GAZE_REACH);
          // A slow wander of about a frame, on two waves that do not share a
          // period — but only once the pointer has been still for a moment.
          // Added while the head is actually FOLLOWING, it fights the follow
          // and the motion reads as jitter.
          var still = Math.min(1, Math.max(0, (now - lastMoveAt - 500) / 900));
          if (still > 0) {
            var tw = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
            aimTarget += still * (Math.sin(tw / 2600) * 0.9 + Math.sin(tw / 1450) * 0.45);
          }
        } else {
          aimTarget = frameForAim(aimFor(dx, dy), headSegs, headMeta.frames);
        }
      } else if (mode === 'track' && now - nearAt > TRACK_LOST_MS) {
        mode = 'return'; aimTarget = headCentre;
      }
      if (mode === 'track' && lastMoveAt && now - lastMoveAt > HOLD_GAZE_MS) {
        // The pointer has been parked a while: relax, and watch again the
        // moment it moves.
        mode = 'return'; aimTarget = headCentre;
      }
    } else if (mode === 'track' && now - nearAt > TRACK_LOST_MS) {
      mode = 'return'; aimTarget = headCentre;
    }
    if (mode !== 'track' && mode !== 'return' && mode !== 'sweep') return;
    if (aimTarget === null) return;
    if (headShown === null) {
      headShown = (player.clip() === 'head') ? player.frame() : headCentre;
    }
    var target = aimTarget;
    // The turn to the right is a separate run of the strip. Coming home from
    // it, walk to ITS front frame and step across there — never back down the
    // whole arc through the overhead frames.
    var right = segEnds(headSegs.right);
    if (right && headShown >= right[0] - 0.5 && target < right[0] - 0.5) {
      if (headShown > right[0] + 0.4) target = right[0];
      else { headShown = headCentre; }
    }
    // One eased walk along the strip, capped at a head's pace. Every frame in
    // between is a real frame of the clip, so the limit buys continuity.
    var k = 1 - Math.exp(-(dt / 1000) / HEAD_TAU);
    var cap = HEAD_MAX_FPS * (dt / 1000);
    var settled = Math.abs(target - headShown) < 0.02;
    headShown = settled ? target : approach(headShown, target, k, cap);
    // Blend into the next frame only WHILE the head is moving: holding a
    // fractional position leaves two poses stacked at partial opacity, which
    // reads as the creature freezing mid-turn (owner, stand 2026-09-12).
    if (settled && target === aimTarget) player.seek('head', Math.round(headShown));
    else player.seekF('head', headShown);
    if (mode === 'return' && settled && target === aimTarget) {
      toIdle();
    }
  }
  function approach(cur, target, k, cap) {
    var d = target - cur;
    if (Math.abs(d) < 0.004) return target;
    var step = d * k;
    if (step > cap) step = cap;
    else if (step < -cap) step = -cap;
    return cur + step;
  }

  // Coming back to the tab: catch the travel up to the wall clock and make
  // sure the animation loop has a frame pending. A tab that was hidden mid
  // arrival could otherwise leave the creature standing where it was (owner,
  // stand 2026-09-12: "I switched tabs and she ran off somewhere").
  function onVisible() {
    if (typeof document !== 'undefined' && document.hidden) return;
    if (!el || gone) return;
    if (travel) updateTravel();
    if (player && player.kick) player.kick();
  }
  function keepHere() {
    if (!el || gone) return;
    noteHere();
    later(keepHere, 2000);
  }
  function onInput(e) {
    lastInput = Date.now();
    if (!el || gone) return;
    if (e && typeof e.clientX === 'number') {
      var prev = ptr;
      ptr = { x: e.clientX, y: e.clientY };
      if (e.type === 'pointermove') { lastMoveAt = Date.now(); feelStroke(prev, ptr); }
    }
    if (mode === 'sleep') toIdle();
  }
  // Being stroked. The pointer has to be ON the creature and MOVING; enough
  // of that within a moment and it leans into the hand with its eyes closed.
  function feelStroke(prev, now) {
    if (!el || gone || !prev || PAUSED) return;
    if (mode === 'busy' || mode === 'out' || mode === 'enter') return;
    var r = el.getBoundingClientRect();
    var inside = now.x >= r.left && now.x <= r.right && now.y >= r.top && now.y <= r.bottom;
    var t = Date.now();
    if (!inside) { strokeAcc = 0; return; }
    if (t - strokeAt > STROKE_WINDOW_MS) strokeAcc = 0;
    strokeAt = t;
    strokeAcc += Math.sqrt((now.x - prev.x) * (now.x - prev.x) + (now.y - prev.y) * (now.y - prev.y));
    if (strokeAcc < STROKE_PX) return;
    strokeAcc = 0;
    if (t - pettedAt < STROKE_COOLDOWN_MS) return;
    pettedAt = t;
    petted();
  }
  function petted() {
    if (!el || gone || !player.has('pet')) return;
    mode = 'busy';
    el.classList.add('is-happy');
    player.play('pet', { onEnd: function () {
      if (!el || gone) return;
      el.classList.remove('is-happy');
      if (mode === 'busy') toIdle();
    } });
  }
  function checkSleep() {
    if (gone || !el) return;
    if (Date.now() - lastInput > SLEEP_AFTER_S * 1000 && mode !== 'sleep' && mode !== 'out' && player.has('sleep')) {
      mode = 'sleep';
      player.play('sleep', { loop: true });
    }
    later(checkSleep, 15000);
  }
  // A click on the fox: it stretches, then settles back — it stays with you.
  function onSelfClick(e) {
    e.stopPropagation();
    if (PAUSED || (panel && panel.contains(e.target))) return;
    if (gone || !el || mode === 'busy' || mode === 'out' || mode === 'enter') return;
    if (!player.has('stretch')) return;
    mode = 'busy';
    el.classList.add('is-happy');
    // Forward and back: the clip ends in the stretched pose, and handing that
    // straight to `idle` popped the body a good 25 px (owner, stand
    // 2026-09-11). Played in reverse it returns to the resting pose it
    // started from, which IS the idle pose.
    var sm = player.meta ? player.meta('stretch') : null;
    var act = sm && sm.segments && segEnds(sm.segments.action);
    var sopts = { pingpong: true };
    // The clip freezes in the stretched pose after the action; the manifest
    // says where the movement ends, so the hold is never played.
    if (act) { sopts.from = Math.min(act[0], act[1]); sopts.to = Math.max(act[0], act[1]); }
    sopts.onEnd = function () {
      if (!el || gone) return;
      el.classList.remove('is-happy');
      if (mode === 'busy') toIdle();
    };
    player.play('stretch', sopts);
  }
  function onKey(e) {
    if (PAUSED || (scene && scene.resident)) return;
    if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === 'k') leave('run');
    if (e.key === 'Escape') leave('run');
  }
  // A click on the page used to send it away. People click constantly — the
  // view switcher, a row, a folder — and every one of those ended the visit
  // for the rest of the day, which reads as the creature getting lost (owner,
  // stand 2026-09-12: "even from table to grid she gets lost somewhere"). Only a
  // click that takes the person OFF this page does now; an overlay is already
  // watched separately.
  function onDocClick(e) {
    if (PAUSED || !el) return;
    // A resident moves WITH the person: clicking through to another section is
    // not it being sent away, and treating it that way meant it ran off and
    // never finished arriving on the next page (owner, 2026-09-12: "I switch
    // pages and she just does not make it").
    if (scene && scene.resident) return;
    if (el.contains(e.target)) return;
    var t = e.target;
    var a = (t && typeof t.closest === 'function') ? t.closest('a[href]') : null;
    if (!a) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || a.getAttribute('target') === '_blank') return;
    leave('run');
  }
  // `how` = 'run' (startled: run_out to the RIGHT, off-screen) | 'walk'
  // (calm, on its own time: the same run_out mirrored, off to the LEFT).
  // Either way nothing of this module stays wired to the page.
  function leave(how) {
    if (gone || !el) return;
    gone = true;
    mode = 'out';
    forgetHere();      // it is leaving on purpose; the next page starts afresh
    travel = null;              // the exit drives the element itself
    clearTimers();
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('click', onDocClick, true);
    document.removeEventListener('visibilitychange', onVisible);
    window.removeEventListener('pagehide', noteHere);
    window.removeEventListener('beforeunload', noteHere);
    document.removeEventListener('pointermove', onInput);
    document.removeEventListener('pointerdown', onInput);
    document.removeEventListener('pointerover', onInput);
    document.removeEventListener('wheel', onInput);
    document.removeEventListener('keydown', onInput);
    el.removeEventListener('click', onSelfClick);
    if (mo) { mo.disconnect(); mo = null; }
    var node = el, pl = player;
    el = null; player = null; sprite = null; ptr = null;
    // The head-tracking flip must go before the exit decides which way the
    // fox faces, or mirror+flip cancel and it moonwalks out (owner, stand
    // 2026-09-11: "she runs out from the left with her head to the right").
    node.classList.remove('is-skid', 'is-happy', 'is-flip', 'is-mirror');
    flipped = false;
    // Both exits travel the way the run strip is drawn, so the exit never
    // mirrors the body either.
    var mir = 'left' !== runFaces;
    node.classList.toggle('is-mirror', mir);
    var spr = node.querySelector('.op-comp-sprite');
    // The ground line still has to be applied while the exit plays — the
    // stand-up clip ramps from the sitting line to the running floor — so the
    // tick stays alive until the element is gone.
    pl.onTick(function () {
      if (!spr) return;
      var dy = groundOffset(pl, spr.clientHeight || 0);
      var css = Math.abs(dy) < 0.4 ? '' : 'translateY(' + dy.toFixed(2) + 'px)';
      if (mir) {
        var cxo = (pl && pl.cxRef) ? pl.cxRef() : 0.5;
        var wo = spr.clientWidth || 0;
        css = (css ? css + ' ' : '') +
              'translateX(' + ((2 * cxo - 1) * wo).toFixed(2) + 'px) scaleX(-1)';
      }
      if (spr.style.transform !== css) { spr.style.transition = ''; spr.style.transform = css; }
    });
    // A startled fox bolts; a calm one trots. Either way it leaves the corner
    // and fades — crossing the whole window at a walking pace took eight
    // seconds, most of it off-screen and pointless.
    var V = how === 'run' ? 0.45 : 0.24;   // px per ms
    var far = -380;
    var runMs = Math.round(Math.abs(far + 60) / V);
    function runOff() {
      node.style.transition = 'transform ' + runMs + 'ms linear, opacity .45s ease ' +
                              Math.max(0, runMs - 450) + 'ms';
      pl.play(pl.has('run_out') ? 'run_out' : 'idle', { loop: true });
      requestAnimationFrame(function () {
        node.style.transform = 'translateX(' + far + 'px)';
        node.style.opacity = '0';
      });
      setTimeout(function () { remove(node, pl); }, runMs + 400);
    }
    // A fox that is sitting cannot simply start running: `standup` is that
    // movement — it gets up, turns to its side and sets off — and the travel
    // eases in over its last half second so the body is already at the gait's
    // speed when the run strip takes over (colleague's review, 2026-09-12:
    // «inspect the reverse transition»).
    if (pl.has('standup')) {
      var upMs = pl.duration('standup') || 900;
      var ACC = 500;                    // ms of acceleration
      var ACC_D = Math.round(V * ACC / 2);   // px covered while accelerating
      var went = false;
      var go = function () { if (!went) { went = true; runOff(); } };
      pl.play('standup', { onEnd: go });
      // requestAnimationFrame stops in a hidden tab, so the clip would never
      // reach its end and the exit would never start; the wall clock finishes
      // it either way (owner, stand 2026-09-12: "while the fox was leaving I
      // switched tabs and she ran off somewhere").
      setTimeout(go, upMs + 250);
      setTimeout(function () {
        node.style.transition = 'transform ' + ACC + 'ms cubic-bezier(.55,0,1,1)';
        node.style.transform = 'translateX(-' + ACC_D + 'px)';
      }, Math.max(0, upMs - ACC));
      setTimeout(function () { remove(node, pl); }, upMs + runMs + 600);
    } else {
      runOff();
    }
  }
  function remove(node, pl) {
    if (pl) pl.destroy();
    if (node && node.parentNode) node.parentNode.removeChild(node);
    // A resident comes back: it was startled off by an overlay, not sent home.
    if (scene && scene.resident && !reborn) {
      reborn = true;
      setTimeout(rebirth, 12000);
    }
  }
  var reborn = false;
  function rebirth() {
    reborn = false;
    if (document.getElementById('op-companion')) return;
    gone = false; el = null; sprite = null; player = null; travel = null;
    mode = 'off'; PAUSED = false; panel = null; scrub = null; picker = null;
    readout = null; lastRead = ''; aimTarget = null; headShown = null;
    lastFacing = ''; flipped = false; ptr = null;
    mount();
  }

  // The manifest is fetched now — after the page gate, after the plan, for an
  // owner only — and the PNG fallback is warmed in parallel so a failed or
  // late manifest still has something to show at the delay.
  function warm() {
    loadSheets();
    ['run', 'sit', 'look'].forEach(function (p) { var i = new Image(); i.src = img(p); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', warm);
  else warm();
  later(mount, scene.delaySec * 1000);
})();
