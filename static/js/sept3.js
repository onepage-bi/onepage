/* "The Third of September" — a one-off joke for 03.09 (Shufutinsky's song).
 * A flip calendar "September 2" → "September 3", a line from the song, and after that
 * every flip lands on the third again. Around it, falling leaves with musical notes.
 * Shown once per user (localStorage), only on September 3,
 * only in the top window (not in dashboard/chat iframes). The overlay is pointer-events:none —
 * clicks pass through; after ~25 seconds everything disappears by itself. */
(function () {
  'use strict';
  if (window.self !== window.top) return;

  var now = new Date();
  if (!(now.getMonth() === 8 && now.getDate() === 3)) return;

  var SEEN_KEY = 'op-sept3-seen-' + now.getFullYear();
  try {
    if (localStorage.getItem(SEEN_KEY)) return;
  } catch (e) { /* without localStorage it shows on every visit — acceptable for one day */ }

  /* start only once the tab is visible: a background tab does not burn the showing */
  if (document.visibilityState === 'hidden') {
    document.addEventListener('visibilitychange', function onVis() {
      if (document.visibilityState === 'visible') {
        document.removeEventListener('visibilitychange', onVis);
        begin();
      }
    });
    return;
  }
  begin();

  function begin() {
  try { localStorage.setItem(SEEN_KEY, '1'); } catch (e) {}

  var css = [
    '#op-sept { position: fixed; inset: 0; z-index: 9000; pointer-events: none; opacity: 1; transition: opacity 1.6s ease; }',
    '#op-sept canvas { position: absolute; inset: 0; width: 100%; height: 100%; }',
    '#op-sept .op-hide { position: absolute; right: 16px; top: 16px; pointer-events: auto;',
    '  font: 12px/1 "Inter Tight", -apple-system, sans-serif; color: #8a6b40;',
    '  background: rgba(253, 250, 243, .85); border: 1px solid rgba(176, 122, 50, .3);',
    '  border-radius: 999px; padding: 7px 14px; cursor: pointer;',
    '  opacity: 0; transition: opacity .8s ease; }',
    '#op-sept .op-hide.show { opacity: 1; }',
    '#op-sept .op-hide:hover { color: #4a3117; border-color: rgba(176, 122, 50, .55); }',
    '#op-sept .op-hide:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }',
    '#op-sept .stage { position: absolute; inset: 0; display: flex; flex-direction: column;',
    '  align-items: center; justify-content: center; gap: 30px; }',
    /* calendar */
    '#op-sept .cal { position: relative; width: 250px; transform: translateY(-60px) scale(.9); opacity: 0;',
    '  transition: transform .75s cubic-bezier(.2,1.4,.4,1), opacity .5s ease;',
    '  filter: drop-shadow(0 18px 28px rgba(70,50,20,.28)); }',
    '#op-sept .cal.in { transform: translateY(0) scale(1); opacity: 1; }',
    '#op-sept .cal.wham { animation: op-sept-wham .6s cubic-bezier(.3,1.8,.4,1); }',
    '@keyframes op-sept-wham {',
    '  0% { transform: translateY(0) scale(1,1) rotate(0deg); }',
    '  25% { transform: translateY(5px) scale(1.02,.95) rotate(-1deg); }',
    '  55% { transform: translateY(-3px) scale(.99,1.02) rotate(.6deg); }',
    '  100% { transform: translateY(0) scale(1,1) rotate(0deg); } }',
    '#op-sept .rings { position: absolute; top: -20px; left: 0; right: 0; display: flex; justify-content: center; gap: 110px; z-index: 5; }',
    '#op-sept .ring { width: 22px; height: 34px; border: 5px solid #8d939c; border-radius: 11px;',
    '  box-shadow: inset 0 2px 2px rgba(255,255,255,.5); }',
    '#op-sept .cap { height: 34px; background: linear-gradient(180deg, #434a56, #333944);',
    '  border-radius: 10px 10px 0 0; position: relative; z-index: 2; }',
    '#op-sept .cap::before, #op-sept .cap::after { content: ""; position: absolute; top: 8px; width: 12px; height: 12px;',
    '  border-radius: 50%; background: #20242c; }',
    '#op-sept .cap::before { left: 59px; } #op-sept .cap::after { right: 59px; }',
    /* page — two halves, folded in the middle (like a flip clock) */
    '#op-sept .pages { position: relative; height: 200px; perspective: 900px; }',
    '#op-sept .pages::after { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 1px;',
    '  background: rgba(90,70,40,.18); z-index: 10; }',
    '#op-sept .half { position: absolute; left: 0; right: 0; height: 50%; overflow: hidden;',
    '  backface-visibility: hidden; -webkit-backface-visibility: hidden; }',
    '#op-sept .half.top { top: 0; transform-origin: 50% 100%; background: linear-gradient(180deg, #fdfaf3, #faf5ea); }',
    '#op-sept .half.bottom { bottom: 0; transform-origin: 50% 0; border-radius: 0 0 12px 12px;',
    '  background: linear-gradient(180deg, #f9f3e6, #f4eddd); box-shadow: inset 0 -2px 6px rgba(120,95,55,.10); }',
    '#op-sept .face { position: absolute; left: 0; right: 0; top: 0; height: 200%;',
    '  display: flex; flex-direction: column; align-items: center; justify-content: center; }',
    '#op-sept .half.bottom .face { top: -100%; }',
    '#op-sept .face .num { font: 700 104px/1 Georgia, "Times New Roman", serif; color: #3d5a8f;',
    '  letter-spacing: -2px; margin-top: 6px; text-shadow: 0 2px 0 rgba(61,90,143,.15); }',
    '#op-sept .face .mon { font: italic 600 26px/1.1 Georgia, serif; color: #b0413e; margin-top: 4px; }',
    '#op-sept .half.fold { z-index: 3; animation: op-sept-fold var(--h, .45s) cubic-bezier(.55,.05,.85,.4) forwards; }',
    '@keyframes op-sept-fold { 0% { transform: rotateX(0deg); filter: brightness(1); }',
    '  100% { transform: rotateX(-90deg); filter: brightness(.75); } }',
    '#op-sept .half.unfold { z-index: 4; transform: rotateX(90deg);',
    '  animation: op-sept-unfold var(--h, .45s) cubic-bezier(.15,.6,.45,1.25) forwards; animation-delay: var(--h, .45s); }',
    '@keyframes op-sept-unfold { 0% { transform: rotateX(90deg); filter: brightness(.75); }',
    '  100% { transform: rotateX(0deg); filter: brightness(1); } }',
    /* song line */
    '#op-sept .quote { text-align: center; }',
    '#op-sept .quote .ln { font: italic 600 40px/1.25 Georgia, "Times New Roman", serif;',
    '  background: linear-gradient(100deg, #6b4312, #a8712a 55%, #7a4d14);',
    '  -webkit-background-clip: text; background-clip: text; color: #6b4312; -webkit-text-fill-color: transparent;',
    '  opacity: 0; transform: translateY(14px); transition: opacity .9s ease, transform .9s ease; }',
    '#op-sept .quote .ln.show { opacity: 1; transform: translateY(0); }',
    '#op-sept .quote .ln + .ln { margin-top: 4px; }',
    '@media (max-width: 640px) {',
    '  #op-sept .cal { width: 200px; }',
    '  #op-sept .rings { gap: 84px; }',
    '  #op-sept .cap::before { left: 46px; } #op-sept .cap::after { right: 46px; }',
    '  #op-sept .pages { height: 165px; }',
    '  #op-sept .face .num { font-size: 82px; }',
    '  #op-sept .face .mon { font-size: 21px; }',
    '  #op-sept .quote .ln { font-size: 26px; }',
    '}'
  ].join('\n');

  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var root = document.createElement('div');
  root.id = 'op-sept';
  root.innerHTML =
    t('<canvas></canvas><div class="stage"><div class="cal"><div class="rings"><div class="ring"></div><div class="ring"></div></div><div class="cap"></div><div class="pages"></div></div><div class="quote" role="status"><div class="ln">Flip the calendar over —</div><div class="ln">and it is the third of September again ♪</div></div></div><button class="op-hide" type="button">Hide ✕</button>');
  document.body.appendChild(root);

  var canvas = root.querySelector('canvas');
  var cal = root.querySelector('.cal');
  var pages = root.querySelector('.pages');
  var lines = root.querySelectorAll('.quote .ln');
  var hideBtn = root.querySelector('.op-hide');
  var stopped = false;
  var timers = [];

  function teardown() {
    stopped = true;
    timers.forEach(function (tv) { clearTimeout(tv); clearInterval(tv); });
    root.remove();
    style.remove();
  }
  hideBtn.addEventListener('click', teardown);

  function at(ms, fn) { timers.push(setTimeout(function () { if (!stopped) fn(); }, ms)); }
  function rand(a, b) { return a + Math.random() * (b - a); }
  function lerp(a, b, k) { return a + (b - a) * k; }

  /* prefers-reduced-motion is ignored on purpose: on corporate PCs/RDP
   * animations are disabled system-wide, not by the person's choice */

  /* ================= canvas: leaves + notes (engine from autumn.js) ================= */
  var ctx = canvas.getContext('2d');
  var W = 0, H = 0, DPR = 1;
  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = root.clientWidth; H = root.clientHeight;
    canvas.width = Math.round(W * DPR);
    canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  resize();
  window.addEventListener('resize', resize);

  var COLORS = [
    ['#e2ab3e', '#b07a1e'], ['#d78d2f', '#a5641c'], ['#c9702a', '#95511e'],
    ['#b1521f', '#7d3a17'], ['#e6c46b', '#b3903c'], ['#a8431f', '#762f15']
  ];
  var NOTE_COLORS = ['#a8712a', '#7a4d14', '#c9702a', '#3d5a8f', '#b0413e'];
  var NOTE_CHARS = ['♪', '♫', '♩', '♬'];

  function drawMaple(g, S, fill, vein) {
    g.beginPath();
    var N = 120;
    for (var i = 0; i <= N; i++) {
      var th = (i / N) * Math.PI * 2 - Math.PI / 2;
      var r = S * (0.34 + 0.56 * Math.pow(Math.abs(Math.cos(th * 2.5)), 0.75)
                        + 0.08 * Math.pow(Math.abs(Math.cos(th * 7.5)), 2));
      var x = Math.cos(th) * r, y = Math.sin(th) * r * 1.05;
      i === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
    }
    g.closePath();
    g.fillStyle = fill; g.fill();
    g.strokeStyle = vein; g.lineWidth = S * 0.045; g.lineCap = 'round';
    g.beginPath();
    for (var k = 0; k < 5; k++) {
      var a = -Math.PI / 2 + (k - 2) * (Math.PI * 2 / 5);
      g.moveTo(0, 0); g.lineTo(Math.cos(a) * S * 0.62, Math.sin(a) * S * 0.62);
    }
    g.moveTo(0, S * 0.55); g.lineTo(0, S * 1.05);
    g.stroke();
  }
  function drawBirch(g, S, fill, vein) {
    g.beginPath();
    g.moveTo(0, -S);
    g.bezierCurveTo(S * 0.85, -S * 0.65, S * 0.72, S * 0.45, 0, S * 0.85);
    g.bezierCurveTo(-S * 0.72, S * 0.45, -S * 0.85, -S * 0.65, 0, -S);
    g.closePath();
    g.fillStyle = fill; g.fill();
    g.strokeStyle = vein; g.lineWidth = S * 0.05; g.lineCap = 'round';
    g.beginPath();
    g.moveTo(0, -S * 0.85); g.lineTo(0, S * 0.8);
    for (var k = 1; k <= 3; k++) {
      var y = -S * 0.55 + k * S * 0.36;
      g.moveTo(0, y); g.lineTo(S * 0.4, y + S * 0.22);
      g.moveTo(0, y); g.lineTo(-S * 0.4, y + S * 0.22);
    }
    g.moveTo(0, S * 0.85); g.lineTo(0, S * 1.2);
    g.stroke();
  }
  function drawOak(g, S, fill, vein) {
    g.save(); g.scale(0.78, 1.05);
    g.beginPath();
    var N = 100;
    for (var i = 0; i <= N; i++) {
      var th = (i / N) * Math.PI * 2 - Math.PI / 2;
      var r = S * (0.62 + 0.28 * Math.abs(Math.cos(th * 3)));
      var x = Math.cos(th) * r, y = Math.sin(th) * r;
      i === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
    }
    g.closePath();
    g.fillStyle = fill; g.fill();
    g.strokeStyle = vein; g.lineWidth = S * 0.055; g.lineCap = 'round';
    g.beginPath(); g.moveTo(0, -S * 0.8); g.lineTo(0, S * 0.85);
    g.moveTo(0, S * 0.85); g.lineTo(0, S * 1.15);
    g.stroke();
    g.restore();
  }

  var SPRITE = 96, SR = SPRITE / 2, LS = SPRITE * 0.34;
  var leafSprites = [], noteSprites = [];
  var SHAPES = [drawMaple, drawBirch, drawOak];
  for (var s = 0; s < SHAPES.length; s++) {
    for (var c = 0; c < COLORS.length; c++) {
      var sc = document.createElement('canvas');
      sc.width = SPRITE; sc.height = SPRITE;
      var g = sc.getContext('2d');
      g.translate(SR, SR);
      SHAPES[s](g, LS, COLORS[c][0], COLORS[c][1]);
      leafSprites.push(sc);
    }
  }
  for (var nc = 0; nc < NOTE_CHARS.length; nc++) {
    for (var cc = 0; cc < NOTE_COLORS.length; cc++) {
      var nsc = document.createElement('canvas');
      nsc.width = SPRITE; nsc.height = SPRITE;
      var ng = nsc.getContext('2d');
      ng.font = '700 ' + Math.round(SPRITE * 0.78) + 'px Georgia, "Times New Roman", serif';
      ng.textAlign = 'center'; ng.textBaseline = 'middle';
      ng.fillStyle = NOTE_COLORS[cc];
      ng.fillText(NOTE_CHARS[nc], SR, SR + SPRITE * 0.04);
      noteSprites.push(nsc);
    }
  }

  var parts = [];
  var t0 = 0, lastT = 0, spawnCarry = 0;

  function makePart(kind, opts) {
    opts = opts || {};
    var dd = Math.pow(Math.random(), 0.8); // depth: 0 — far, 1 — near
    var isNote = kind === 'note';
    var rv = isNote ? rand(-0.5, 0.5) : rand(-1.4, 1.4);
    return {
      note: isNote,
      d: dd,
      x: opts.x != null ? opts.x : rand(-80, W + 40),
      y: opts.y != null ? opts.y : rand(-90, -40),
      size: (isNote ? lerp(12, 26, dd) : lerp(11, 34, dd)) * rand(0.85, 1.15),
      vy: (isNote ? lerp(18, 60, dd) : lerp(26, 88, dd)) * rand(0.85, 1.2),
      swayAmp: lerp(12, 42, dd) * rand(0.7, 1.3),
      swayFreq: rand(0.35, 0.8),
      swayPh: rand(0, Math.PI * 2),
      rot: isNote ? rand(-0.4, 0.4) : rand(0, Math.PI * 2),
      rotV: rv, rotV0: rv,
      tumbleFreq: isNote ? 0 : rand(0.4, 1.4),
      tumblePh: rand(0, Math.PI * 2),
      alpha: lerp(0.55, 0.95, dd),
      sprite: isNote ? noteSprites[(Math.random() * noteSprites.length) | 0]
                     : leafSprites[(Math.random() * leafSprites.length) | 0],
      ivx: opts.ivx || 0, ivy: opts.ivy || 0
    };
  }

  /* a volley of notes from under the calendar on the flip */
  function burstNotes(count) {
    var r = cal.getBoundingClientRect();
    for (var i = 0; i < count; i++) {
      var side = i % 2 === 0 ? -1 : 1;
      parts.push(makePart('note', {
        x: r.left + r.width / 2 + side * rand(r.width * 0.25, r.width * 0.55),
        y: r.top + rand(20, r.height - 20),
        ivx: side * rand(120, 320),
        ivy: -rand(260, 520)
      }));
    }
  }

  /* the cursor scatters the leaves */
  var mouse = { x: -1e4, y: -1e4, spd: 0, lx: 0, ly: 0, lt: 0 };
  function onPointer(x, y) {
    var pnow = performance.now() / 1000;
    var pdt = pnow - mouse.lt;
    if (pdt > 0 && pdt < 0.2 && mouse.lx) {
      var sp = Math.hypot(x - mouse.lx, y - mouse.ly) / pdt;
      mouse.spd = Math.min(sp, 2200);
    }
    mouse.lx = x; mouse.ly = y; mouse.lt = pnow;
    mouse.x = x; mouse.y = y;
  }
  window.addEventListener('mousemove', function (e) { onPointer(e.clientX, e.clientY); }, { passive: true });
  window.addEventListener('touchmove', function (e) {
    if (e.touches[0]) onPointer(e.touches[0].clientX, e.touches[0].clientY);
  }, { passive: true });

  /* intensity: the leaf-fall with notes starts with the calendar flip,
     holds, dies down; once everything has landed the overlay disappears */
  var SKY_START = 2.4, SPAWN_END = 22;
  function spawnRate(tv) {
    if (tv < SKY_START) return 0;
    if (tv < SKY_START + 5) return lerp(0, 9, (tv - SKY_START) / 5);
    if (tv < 15) return 9;
    if (tv < SPAWN_END) return lerp(9, 0, (tv - 15) / (SPAWN_END - 15));
    return 0;
  }
  function wind(tv) {
    var ramp = Math.min(1, Math.max(0, (tv - SKY_START) / 5));
    var gust = 0.6 + 0.4 * Math.sin(tv * 0.31) * Math.sin(tv * 0.13 + 2);
    return ramp * gust;
  }

  var fading = false;

  function frame(nowMs) {
    if (stopped) return;
    var now = nowMs / 1000;
    if (!t0) { t0 = now; lastT = now; }
    var tv = now - t0;
    var dt = Math.min(now - lastT, 0.05);
    lastT = now;

    spawnCarry += spawnRate(tv) * dt;
    while (spawnCarry >= 1 && parts.length < 160) {
      parts.push(makePart(Math.random() < 0.4 ? 'note' : 'leaf'));
      spawnCarry -= 1;
    }
    spawnCarry = Math.min(spawnCarry, 3);

    var wnd = wind(tv);
    mouse.spd *= Math.exp(-3 * dt);
    ctx.clearRect(0, 0, W, H);

    for (var i = parts.length - 1; i >= 0; i--) {
      var L = parts[i];
      var mdx = L.x - mouse.x, mdy = L.y - mouse.y;
      var MR = 130;
      var md2 = mdx * mdx + mdy * mdy;
      if (md2 < MR * MR && md2 > 1) {
        var md = Math.sqrt(md2);
        var kf = (1 - md / MR);
        var push = (140 + mouse.spd * 0.45) * kf * lerp(0.5, 1.15, L.d);
        L.ivx += (mdx / md) * push * dt * 6;
        L.ivy += ((mdy / md) * push - 90 * kf) * dt * 6;
        L.rotV += rand(-14, 14) * kf * dt;
      }
      var damp = Math.exp(-2.6 * dt);
      L.ivx *= damp; L.ivy *= damp;

      L.y += (L.vy + L.ivy) * dt;
      L.x += (wnd * lerp(8, 55, L.d)
              + Math.cos(now * L.swayFreq * Math.PI * 2 + L.swayPh) * L.swayAmp * L.swayFreq * 2
              + L.ivx) * dt;
      L.rot += L.rotV * dt;
      L.rotV += (L.rotV0 - L.rotV) * Math.min(1, 1.4 * dt);
      if (L.y > H + 60 || L.x > W + 120 || L.x < -140) { parts.splice(i, 1); continue; }

      var tumble = L.note ? 1 : 0.32 + 0.68 * Math.abs(Math.sin(now * L.tumbleFreq * Math.PI * 2 + L.tumblePh));
      ctx.globalAlpha = L.alpha;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.translate(L.x, L.y);
      ctx.rotate(L.rot);
      ctx.scale(tumble, 1);
      var sz = L.size;
      ctx.drawImage(L.sprite, -sz, -sz, sz * 2, sz * 2);
    }
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.globalAlpha = 1;

    /* everything has landed — fade the overlay out */
    if (!fading && tv > SPAWN_END && parts.length === 0) {
      fading = true;
      root.style.opacity = '0';
      setTimeout(function () { if (!stopped) teardown(); }, 1800);
    }

    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  /* ================= calendar ================= */
  var current = null;
  function makeHalf(pos, num, mon) {
    var h = document.createElement('div');
    h.className = 'half ' + pos;
    h.innerHTML = '<div class="face"><div class="num"></div><div class="mon"></div></div>';
    h.querySelector('.num').textContent = num;
    h.querySelector('.mon').textContent = mon;
    return h;
  }
  function setStatic(num, mon) {
    pages.innerHTML = '';
    pages.appendChild(makeHalf('top', num, mon));
    pages.appendChild(makeHalf('bottom', num, mon));
    current = [num, mon];
  }

  var flipping = false;
  function flipTo(num, mon, dur) {
    if (stopped || fading || flipping || !current) return;
    flipping = true;
    var half = Math.round(dur / 2) + 'ms';
    /* static backing: the new date already on top, the old one still at the bottom */
    var topNext = makeHalf('top', num, mon);
    var bottomCur = makeHalf('bottom', current[0], current[1]);
    /* moving halves: the old top folds down, the new bottom unfolds */
    var topCur = makeHalf('top', current[0], current[1]);
    topCur.classList.add('fold');
    topCur.style.setProperty('--h', half);
    var bottomNext = makeHalf('bottom', num, mon);
    bottomNext.classList.add('unfold');
    bottomNext.style.setProperty('--h', half);
    pages.innerHTML = '';
    pages.appendChild(topNext);
    pages.appendChild(bottomCur);
    pages.appendChild(topCur);
    pages.appendChild(bottomNext);
    at(dur, function () {
      setStatic(num, mon);
      flipping = false;
      cal.classList.remove('wham');
      void cal.offsetWidth;
      cal.classList.add('wham');
      burstNotes(10);
    });
  }

  setStatic('2', t('September {ctx:2}'));

  at(300, function () { cal.classList.add('in'); });
  at(1600, function () { flipTo('3', t('September {ctx:2}'), 900); });
  at(2200, function () { hideBtn.classList.add('show'); });
  at(3000, function () { lines[0].classList.add('show'); });
  at(3900, function () { lines[1].classList.add('show'); });
  /* the meme's joke: flip further — and it is September the third again */
  at(5800, function () {
    var loop = setInterval(function () {
      if (stopped) return;
      flipTo('3', t('September {ctx:2}'), 900);
    }, 4200);
    timers.push(loop);
  });
  }
})();
