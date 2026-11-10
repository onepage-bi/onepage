/* Dev Guide (/docs) — three small behaviours, no dependencies.
   1. Two reading tracks: the page shows the steps of one track at a time.
      The choice rides in the URL (?track=) so a link can point a colleague
      at the right half, and is remembered per person in localStorage.
   2. The platform map doubles as navigation: a node opens the track it
      belongs to and scrolls to its step.
   3. Copy buttons on every artifact, and click-to-zoom on every screenshot —
      the shots are full product screens and unreadable at column width. */
(function () {
  'use strict';

  var root = document.getElementById('dg-root');
  if (!root) return;

  var KEY = 'op-docs-track';
  var TRACKS = ['analyst', 'author'];

  function setTrack(name, push) {
    if (TRACKS.indexOf(name) < 0) name = TRACKS[0];
    root.setAttribute('data-track', name);
    document.querySelectorAll('[data-track-btn]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-track-btn') === name ? 'true' : 'false');
    });
    try { localStorage.setItem(KEY, name); } catch (e) {}
    if (push && window.history && history.replaceState) {
      var u = new URL(window.location.href);
      u.searchParams.set('track', name);
      history.replaceState(null, '', u.toString());
    }
  }

  var fromUrl = new URLSearchParams(window.location.search).get('track');
  var stored = null;
  try { stored = localStorage.getItem(KEY); } catch (e) {}
  setTrack(fromUrl || stored || 'analyst', false);

  document.querySelectorAll('[data-track-btn]').forEach(function (b) {
    b.addEventListener('click', function () { setTrack(b.getAttribute('data-track-btn'), true); });
  });

  /* Map node → the step it describes. A node may belong to a track that is
     not on screen, so switch first and scroll after the layout settles. */
  document.querySelectorAll('[data-goto]').forEach(function (n) {
    n.addEventListener('click', function () {
      var id = n.getAttribute('data-goto');
      var want = n.getAttribute('data-goto-track');
      if (want) setTrack(want, true);
      var el = document.getElementById(id);
      if (!el) return;
      requestAnimationFrame(function () {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  });

  /* Copy: the artifact is the point of the block, so the button says what it
     copied rather than just "copied". */
  document.querySelectorAll('.dg-copy').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var pre = document.getElementById(btn.getAttribute('data-copy'));
      if (!pre) return;
      var text = pre.innerText;
      var done = function () {
        var was = btn.textContent;
        btn.textContent = t('Copied');
        btn.setAttribute('data-done', '1');
        setTimeout(function () {
          btn.textContent = was;
          btn.removeAttribute('data-done');
        }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
        return;
      }
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (e) {}
      document.body.removeChild(ta);
    });
  });

  /* Zoom */
  var zoom = null;
  function closeZoom() {
    if (!zoom) return;
    zoom.remove();
    zoom = null;
    document.removeEventListener('keydown', onKey);
  }
  function onKey(e) { if (e.key === 'Escape') closeZoom(); }
  document.querySelectorAll('.dg-art img').forEach(function (img) {
    img.addEventListener('click', function () {
      closeZoom();
      zoom = document.createElement('div');
      zoom.className = 'dg-zoom';
      var big = document.createElement('img');
      big.src = img.src;
      big.alt = img.alt;
      zoom.appendChild(big);
      zoom.addEventListener('click', closeZoom);
      document.body.appendChild(zoom);
      document.addEventListener('keydown', onKey);
    });
  });
})();
