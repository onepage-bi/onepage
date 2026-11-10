/* Falling leaves for September 1 — a one-off greeting.
 * Shown once per user (localStorage), only on 31.08 and 01.09,
 * only in the top window (not in dashboard/chat iframes). The overlay is pointer-events:none —
 * clicks pass through; after ~50 seconds the leaves finish and everything disappears by itself. */
(function () {
  'use strict';
  if (window.self !== window.top) return;

  var now = new Date();
  var m = now.getMonth(), d = now.getDate();
  if (!((m === 7 && d === 31) || (m === 8 && d === 1))) return;

  /* suffix -2: the first version of the patch set the flag even without showing — reset it for everyone */
  var SEEN_KEY = 'op-autumn-seen-' + now.getFullYear() + '-2';
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
    '#op-autumn { position: fixed; inset: 0; z-index: 9000; pointer-events: none; opacity: 1; transition: opacity 1.6s ease; }',
    '#op-autumn canvas { position: absolute; inset: 0; width: 100%; height: 100%; }',
    '#op-autumn .op-greet { position: absolute; left: 50%; top: 36%; transform: translate(-50%, -50%);',
    '  text-align: center; opacity: 0; filter: blur(8px); transition: opacity 2s ease, filter 2s ease;',
    '  padding: 90px 130px;',
    '  background: radial-gradient(ellipse 55% 50% at center,',
    '    rgba(255, 251, 242, .96) 0%, rgba(255, 251, 242, .8) 45%, rgba(255, 251, 242, 0) 72%); }',
    '#op-autumn .op-greet.show { opacity: 1; filter: blur(0); }',
    '#op-autumn .op-greet .title { font: italic 400 58px/1.12 Georgia, "Times New Roman", serif;',
    '  max-width: 13em; letter-spacing: .5px; text-wrap: balance;',
    '  background: linear-gradient(160deg, #6b4312 20%, #a8712a 55%, #7a4d14 85%);',
    '  -webkit-background-clip: text; background-clip: text;',
    '  color: #6b4312; -webkit-text-fill-color: transparent; }',
    '#op-autumn .op-hide { position: absolute; right: 16px; top: 16px; pointer-events: auto;',
    '  font: 12px/1 "Inter Tight", -apple-system, sans-serif; color: #8a6b40;',
    '  background: rgba(253, 250, 243, .85); border: 1px solid rgba(176, 122, 50, .3);',
    '  border-radius: 999px; padding: 7px 14px; cursor: pointer;',
    '  opacity: 0; transition: opacity .8s ease; }',
    '#op-autumn .op-hide.show { opacity: 1; }',
    '#op-autumn .op-hide:hover { color: #4a3117; border-color: rgba(176, 122, 50, .55); }',
    '#op-autumn .op-hide:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }',
    '@media (max-width: 640px) {',
    '  #op-autumn .op-greet { padding: 60px 30px; width: 100%; box-sizing: border-box; }',
    '  #op-autumn .op-greet .title { font-size: 38px; }',
    '}'
  ].join('\n');

  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var root = document.createElement('div');
  root.id = 'op-autumn';
  root.innerHTML =
    t('<canvas></canvas><div class="op-greet" role="status"><div class="title">Happy autumn!</div></div><button class="op-hide" type="button">Hide falling leaves ✕</button>');
  document.body.appendChild(root);

  var canvas = root.querySelector('canvas');
  var greet = root.querySelector('.op-greet');
  var hideBtn = root.querySelector('.op-hide');
  var stopped = false;

  function teardown() {
    stopped = true;
    root.remove();
    style.remove();
  }
  hideBtn.addEventListener('click', teardown);

  /* prefers-reduced-motion is ignored on purpose: on corporate PCs/RDP
   * animations are disabled system-wide, not by the person's choice — nobody would see the leaf-fall */

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

  /* ---------- Leaf sprites ---------- */
  var COLORS = [
    ['#e2ab3e', '#b07a1e'], ['#d78d2f', '#a5641c'], ['#c9702a', '#95511e'],
    ['#b1521f', '#7d3a17'], ['#e6c46b', '#b3903c'], ['#a8431f', '#762f15']
  ];

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

  var SHAPES = [drawMaple, drawBirch, drawOak];
  var SPRITE = 96, SR = SPRITE / 2, LS = SPRITE * 0.34;
  var sprites = [];
  for (var s = 0; s < SHAPES.length; s++) {
    for (var c = 0; c < COLORS.length; c++) {
      var sc = document.createElement('canvas');
      sc.width = SPRITE; sc.height = SPRITE;
      var g = sc.getContext('2d');
      g.translate(SR, SR);
      SHAPES[s](g, LS, COLORS[c][0], COLORS[c][1]);
      sprites.push(sc);
    }
  }

  /* ---------- Physics ---------- */
  var leaves = [];
  var t0 = 0;      /* counted from the first real frame */
  var lastT = 0;
  var spawnCarry = 0;

  function rand(a, b) { return a + Math.random() * (b - a); }
  function lerp(a, b, k) { return a + (b - a) * k; }

  function makeLeaf(hero) {
    var dd = hero ? 0.85 : Math.pow(Math.random(), 0.8); // depth: 0 — far, 1 — near
    var rv = rand(-1.4, 1.4);
    return {
      d: dd,
      x: hero ? -60 : rand(-80, W + 40),
      y: hero ? H * rand(0.12, 0.25) : rand(-90, -40),
      size: lerp(11, 34, dd) * rand(0.85, 1.15) * (hero ? 1.25 : 1),
      vy: lerp(26, 88, dd) * rand(0.85, 1.2) * (hero ? 0.55 : 1),
      swayAmp: lerp(12, 42, dd) * rand(0.7, 1.3),
      swayFreq: rand(0.35, 0.8),
      swayPh: rand(0, Math.PI * 2),
      rot: rand(0, Math.PI * 2),
      rotV: rv,
      rotV0: rv,
      tumbleFreq: rand(0.4, 1.4),
      tumblePh: rand(0, Math.PI * 2),
      alpha: lerp(0.55, 0.95, dd),
      sprite: sprites[(Math.random() * sprites.length) | 0],
      hero: !!hero,
      ivx: 0, ivy: 0
    };
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

  /* intensity curve: silence → a single leaf → a gust → dying down → end */
  var SPAWN_END = 46;
  function spawnRate(tv) {
    if (tv < 3) return 0;
    if (tv < 9) return lerp(0, 11, (tv - 3) / 6);      // wind picks up
    if (tv < 24) return 11;                            // peak
    if (tv < 40) return lerp(11, 1.4, (tv - 24) / 16);  // dying down
    if (tv < SPAWN_END) return 1.4;                    // last leaves
    return 0;
  }

  function wind(tv) {
    var ramp = Math.min(1, Math.max(0, (tv - 2.5) / 6));
    var gust = 0.6 + 0.4 * Math.sin(tv * 0.31) * Math.sin(tv * 0.13 + 2);
    return ramp * gust;
  }

  var heroSpawned = false;
  var fading = false;

  function frame(nowMs) {
    if (stopped) return;
    var now = nowMs / 1000;
    if (!t0) { t0 = now; lastT = now; }
    var tv = now - t0;
    var dt = Math.min(now - lastT, 0.05);
    lastT = now;

    if (!heroSpawned && tv > 0.8) { leaves.push(makeLeaf(true)); heroSpawned = true; }

    spawnCarry += spawnRate(tv) * dt;
    while (spawnCarry >= 1 && leaves.length < 150) {
      leaves.push(makeLeaf(false));
      spawnCarry -= 1;
    }
    spawnCarry = Math.min(spawnCarry, 3);

    var wnd = wind(tv);
    mouse.spd *= Math.exp(-3 * dt);
    ctx.clearRect(0, 0, W, H);

    for (var i = leaves.length - 1; i >= 0; i--) {
      var L = leaves[i];
      /* scatter away from the cursor */
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
              + L.ivx) * dt
              * (L.hero ? 1.8 : 1);
      L.rot += L.rotV * dt;
      /* spin from the cursor smoothly returns to the leaf's own */
      L.rotV += (L.rotV0 - L.rotV) * Math.min(1, 1.4 * dt);
      if (L.y > H + 60 || L.x > W + 120) { leaves.splice(i, 1); continue; }

      var tumble = 0.32 + 0.68 * Math.abs(Math.sin(now * L.tumbleFreq * Math.PI * 2 + L.tumblePh));
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

    /* all leaves have fallen — fade the overlay out */
    if (!fading && tv > SPAWN_END && leaves.length === 0) {
      fading = true;
      root.style.opacity = '0';
      setTimeout(function () { if (!stopped) teardown(); }, 1800);
    }

    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  /* greeting: appears at the peak of the wind, leaves by itself */
  setTimeout(function () { if (!stopped) greet.classList.add('show'); }, 5000);
  setTimeout(function () { if (!stopped) greet.classList.remove('show'); }, 13000);
  setTimeout(function () { if (!stopped) hideBtn.classList.add('show'); }, 6000);
  }
})();
