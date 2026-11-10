/* Collection season opening: a short story on entering the dashboards list.
 *
 * Shown ONCE per person (localStorage) and only on "/" — not on
 * a dashboard page, not in a presentation, not inside an iframe. It costs the server
 * nothing: the mark lives in the browser, there are no requests.
 *
 * Sound is off on purpose and it is not over-caution: browsers block
 * autoplay with sound, and in an open space it is tactless. The unmute button is right there.
 *
 * With the system "reduce motion" setting the video does not start at all —
 * a poster frame and a "Watch" button are shown instead. Half of our people
 * work on VDI, where animation is disabled by policy, and five megabytes of video will not
 * play for them; such a person must see the story, not emptiness.
 */
(function () {
  'use strict';
  if (window.self !== window.top) return;
  if (location.pathname !== '/') return;

  var root = document.getElementById('op-season-intro');
  if (!root) return;                       // template did not render it — the season is not open

  var KEY = 'op-season-intro-' + (root.getAttribute('data-season') || '?') + '-' +
            String((window.OP_USER && window.OP_USER.person_id) || 'anon');
  try {
    if (localStorage.getItem(KEY)) return;
  } catch (e) { /* without localStorage it is shown again — one story, no harm */ }

  var t = (window.OP_I18N && window.OP_I18N.t) ? window.OP_I18N.t : function (s) { return s; };

  var reduced = false;
  try { reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}

  function seen() {
    try { localStorage.setItem(KEY, String(Date.now())); } catch (e) {}
  }

  // ── Petals ────────────────────────────────────────────────────────────
  // The video ends on an almost black frame, and without them the finale falls into
  // a dark rectangle. The petals fly over the dimmer for the whole showing
  // and a couple of seconds after closing — that is what softens the transition to the light
  // page.
  //
  // `prefers-reduced-motion` deliberately does NOT switch them off here entirely, it
  // only thins and slows them: on corporate machines and VDI animation is
  // disabled by policy, not by the person's choice — the same argument recorded in
  // autumn.js. The leaf-fall engine cannot be borrowed: it is pinned to August 31 and
  // draws maples and oaks.
  var petals = (function () {
    var cv = root.querySelector('.si-petals');
    if (!cv || !cv.getContext) return { start: function () {}, stop: function () {} };
    var ctx = cv.getContext('2d');
    var W = 0, H = 0, DPR = 1, items = [], raf = 0, last = 0, dying = 0;
    var COUNT = reduced ? 14 : 38;

    function resize() {
      DPR = Math.min(window.devicePixelRatio || 1, 2);
      W = root.clientWidth || window.innerWidth;
      H = root.clientHeight || window.innerHeight;
      cv.width = Math.round(W * DPR);
      cv.height = Math.round(H * DPR);
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    }

    function rnd(a, b) { return a + Math.random() * (b - a); }

    function make(seeded) {
      return {
        x: rnd(-40, W + 40),
        // On the first run they are scattered over the whole height, otherwise for the first seconds
        // the screen is empty and then a wall pours down from the top.
        y: seeded ? rnd(-H, H) : rnd(-80, -10),
        s: rnd(6, 13),
        vy: rnd(18, 46) * (reduced ? 0.55 : 1),
        vx: rnd(-10, 26) * (reduced ? 0.55 : 1),
        a: rnd(0, Math.PI * 2),
        va: rnd(-1.1, 1.1) * (reduced ? 0.3 : 1),
        sw: rnd(0.5, 1.6),
        o: rnd(0.55, 0.95),
      };
    }

    // A petal: half a sakura leaf — two curves and a notch at the tip.
    function draw(p) {
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.a);
      // The sideways turn is imitated by squeezing the width: honest 3D is not needed here.
      // The multiplier NEVER reaches zero — otherwise the petal degenerates into a
      // thin dash and on a dark frame reads as a scratch (seen on the
      // very first screenshot). Take the absolute value and keep the floor at 0.45.
      ctx.scale(Math.abs(Math.cos(p.a * 0.7)) * 0.55 + 0.45, 1);
      ctx.globalAlpha = p.o;
      ctx.beginPath();
      ctx.moveTo(0, -p.s);
      ctx.bezierCurveTo(p.s * 0.8, -p.s * 0.5, p.s * 0.6, p.s * 0.7, 0, p.s);
      ctx.bezierCurveTo(-p.s * 0.6, p.s * 0.7, -p.s * 0.8, -p.s * 0.5, 0, -p.s);
      ctx.fillStyle = '#f7cfdd';
      ctx.fill();
      ctx.restore();
    }

    function tick(now) {
      raf = window.requestAnimationFrame(tick);
      var dt = last ? Math.min((now - last) / 1000, 0.05) : 0.016;
      last = now;
      ctx.clearRect(0, 0, W, H);
      var fade = dying ? Math.max(0, 1 - (now - dying) / 2600) : 1;
      if (dying && fade <= 0) { stop(); return; }
      ctx.globalAlpha = 1;
      for (var i = 0; i < items.length; i++) {
        var p = items[i];
        p.y += p.vy * dt;
        p.x += (p.vx + Math.sin(p.y / 46) * 12 * p.sw) * dt;
        p.a += p.va * dt;
        if (p.y > H + 30) {
          if (dying) continue;
          items[i] = make(false);
          continue;
        }
        var keep = p.o;
        p.o = keep * fade;
        draw(p);
        p.o = keep;
      }
    }

    function start() {
      resize();
      items = [];
      for (var i = 0; i < COUNT; i++) items.push(make(true));
      window.addEventListener('resize', resize);
      last = 0;
      raf = window.requestAnimationFrame(tick);
    }

    function stop() {
      if (raf) window.cancelAnimationFrame(raf);
      raf = 0;
      window.removeEventListener('resize', resize);
      try { ctx.clearRect(0, 0, W, H); } catch (e) {}
    }

    return {
      start: start,
      stop: stop,
      // Do not cut off mid-sentence: petals already flying across the screen live out
      // their time and fade — that is the whole point.
      farewell: function () { dying = window.performance ? performance.now() : Date.now(); },
    };
  })();

  function close() {
    seen();
    document.removeEventListener('keydown', onKey, true);
    // First the video card leaves, the dimmer fades more slowly, and the petals
    // keep flying: exactly this tail removes the "black ending".
    root.classList.add('is-fading');
    petals.farewell();
    window.setTimeout(function () {
      root.classList.remove('is-on');
      petals.stop();
      root.remove();
    }, 2800);
  }

  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
  }

  var video = root.querySelector('.si-video');
  var poster = root.querySelector('.si-poster');
  var btnSkip = root.querySelector('.si-skip');
  var btnSound = root.querySelector('.si-sound');
  var btnPlay = root.querySelector('.si-play');

  btnSkip.addEventListener('click', close);
  root.querySelector('.si-dim').addEventListener('click', close);
  document.addEventListener('keydown', onKey, true);

  // The video ended by itself — that is what "watched" means. Do not close instantly:
  // the last frame holds for a second, otherwise the story breaks off mid-sentence.
  video.addEventListener('ended', function () {
    window.setTimeout(close, 1000);
  });

  btnSound.addEventListener('click', function () {
    video.muted = !video.muted;
    btnSound.textContent = video.muted ? t('Sound on') : t('Sound off');
  });

  function play() {
    poster.hidden = true;
    video.hidden = false;
    btnSound.hidden = false;
    btnPlay.hidden = true;
    var p = video.play();
    // Autoplay may be blocked even without sound — then honestly show
    // the poster with a button, not a black rectangle.
    if (p && typeof p.catch === 'function') {
      p.catch(function () {
        video.hidden = true;
        poster.hidden = false;
        btnPlay.hidden = false;
      });
    }
  }

  btnPlay.addEventListener('click', play);

  root.classList.add('is-on');
  petals.start();
  if (reduced) {
    poster.hidden = false;
    btnPlay.hidden = false;
  } else {
    play();
  }
})();
