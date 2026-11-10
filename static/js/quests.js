/* OnePage — quests global (badge FAB + cross-page client events).
 * Mounted on every shell page (see base.html, gated by settings.quests_enabled
 * and window.OP_USER), never on /quests itself and never inside an iframe
 * (dashboard viewer / presentation). Same gating idiom as autumn.js:7-29.
 *
 * Badge (Dashboards tab only): localStorage['op-quests-badge-<uid>'] = {date, open} caches the server's
 * answer for the day (server date wins, not the client's). A fresh cache with
 * open===true means "already opened today" — nothing to show, skip the fetch.
 * Otherwise, after DOMContentLoaded, GET /api/quests/badge; open===false shows
 * a floating "Surprise of the day" pill linking to /quests.
 *
 * Client events: a delegated capture-phase click listener recognizes two
 * quest-eligible actions elsewhere in the app — toggling the theme via the
 * command palette, and switching the dashboard grid to card view — and posts
 * them once per browser via POST /api/quests/event. */
(function () {
  'use strict';

  // Captured once, at IIFE top-level, before any later `document.currentScript`
  // (e.g. another <script> parsing while this one is still deferred) can
  // clear it out from under assetV().
  var SCRIPT_SRC = (typeof document !== 'undefined' && document.currentScript && document.currentScript.src) || '';

  function pad2(n) { return n < 10 ? '0' + n : '' + n; }

  // Local calendar date as YYYY-MM-DD (client-side "today", used only to
  // decide whether the cached badge answer is worth reusing before hitting
  // the network — the server's own `date` is what actually gets cached).
  // The Dashboards page only — see the note in quests-companion.js.
  var COMPANION_PAGES_EXPORT = ['/'];
  function companionPageExport(path) {
    var p = String(path || '/');
    if (p.length > 1 && p.charAt(p.length - 1) === '/') p = p.slice(0, -1);
    return COMPANION_PAGES_EXPORT.indexOf(p) >= 0;
  }

  function localDateStr(d) {
    d = d || new Date();
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  // Is a cached badge answer still good enough to skip the fetch? A positive
  // cache (open===true) is good for the rest of the calendar day — nothing
  // will "un-open" the daily chest. A negative cache (open===false, i.e.
  // "checked today, not open yet") is only good for NEG_TTL_MS from the
  // moment it was checked, so the badge still appears once the day's window
  // opens without waiting for a full page reload the next day.
  var NEG_TTL_MS = 45 * 60 * 1000;
  function badgeFresh(cache, today, now) {
    if (!cache || cache.date !== today) return false;
    if (cache.open !== true && cache.open !== false) return false;
    // Both answers age out within the day: a positive one because a quest
    // reward can add a pending surprise later, a negative one to keep the
    // request count low. A cache without a timestamp is treated as stale.
    now = now == null ? Date.now() : now;
    return typeof cache.checkedAt === 'number' && (now - cache.checkedAt) < NEG_TTL_MS;
  }

  // Is the cached companion answer still good? The date stored in it is the
  // SERVER's day — quests run on one timezone — so comparing it only with the
  // browser's day hid the creature from everyone whose machine sits on
  // another timezone, for as long as the two days differed (found on the
  // stand 2026-09-12, when the server's day rolled over first).
  var COMPANION_TTL_MS = 20 * 3600 * 1000;
  function companionFreshExport(info, today, now) {
    if (!info) return false;
    if (info.date === today) return true;
    var t = +info.checkedAt;
    now = now == null ? Date.now() : now;
    return isFinite(t) && t > 0 && (now - t) < COMPANION_TTL_MS;
  }

  // Which quest event (if any) a click target corresponds to.
  function eventKindFor(target) {
    if (!target || typeof target.closest !== 'function') return null;
    if (target.closest('[data-action="toggle:theme"]')) return 'theme';
    if (target.closest('[data-view-btn="grid"]')) return 'view';
    return null;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { localDateStr, badgeFresh, eventKindFor, companionPage: companionPageExport,
                      companionFresh: companionFreshExport };
  }
  if (typeof document === 'undefined') return; // node require() path stops here

  if (window.self !== window.top) return;
  if (!window.OP_USER) return;
  // On /quests itself the page (quests-page.js) owns the badge, the FAB and
  // the celebration — but the EVENT HOOKS below must stay alive there too:
  // the command palette on that page is the real one, and a user who follows
  // "Show me how" with a real Ctrl+K → "Toggle theme" right there got nothing
  // counted (prod, 2026-09-07). The page is told through `op:quest-award`.
  var ON_QUESTS = location.pathname === '/quests' || location.pathname.startsWith('/quests/');

  // Keys are per user: one browser, several dev/shared accounts must not share a cache.
  var UID = String((window.OP_USER && window.OP_USER.person_id) || 'anon');
  var BADGE_KEY = 'op-quests-badge-' + UID;

  function readBadgeCache() {
    try {
      var raw = localStorage.getItem(BADGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }
  function writeBadgeCache(v) {
    try { localStorage.setItem(BADGE_KEY, JSON.stringify(v)); } catch (e) {}
  }

  function assetV() {
    var m = /[?&]v=([^&]+)/.exec(SCRIPT_SRC);
    return m ? m[1] : '1';
  }

  function mountFab() {
    if (document.getElementById('op-quests-fab')) return;
    var style = document.createElement('style');
    style.textContent = [
      '#op-quests-fab { position: fixed; right: 20px; bottom: 88px; z-index: 60;',
      '  display: flex; align-items: center; gap: 10px; padding: 10px 16px 10px 10px;',
      '  border-radius: 999px; text-decoration: none; color: #3a2a00;',
      '  background: linear-gradient(135deg, #ffe9a8, #ffd166);',
      '  box-shadow: 0 6px 20px rgba(180, 130, 10, 0.35), 0 0 0 0 rgba(255, 209, 102, 0.6);',
      '  animation: op-quests-bob 2.6s ease-in-out infinite, op-quests-ring 2.6s ease-out infinite; }',
      // Below any open .overlay (z:80+, main.css), so a modal never has this
      // pill floating over it. :has() is the primary mechanism; a JS
      // fallback (toggleFabForOverlays below) covers browsers without it.
      'body:has(.overlay.visible) #op-quests-fab { display: none; }',
      '#op-quests-fab img { width: 34px; height: 34px; display: block; }',
      '#op-quests-fab span { display: flex; flex-direction: column; line-height: 1.2; font-weight: 600; font-size: 13px; }',
      '#op-quests-fab span small { font-weight: 400; font-size: 11px; opacity: 0.75; }',
      '@keyframes op-quests-bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-4px); } }',
      '@keyframes op-quests-ring { 0% { box-shadow: 0 6px 20px rgba(180, 130, 10, 0.35), 0 0 0 0 rgba(255, 209, 102, 0.55); }',
      '  70% { box-shadow: 0 6px 20px rgba(180, 130, 10, 0.35), 0 0 0 12px rgba(255, 209, 102, 0); }',
      '  100% { box-shadow: 0 6px 20px rgba(180, 130, 10, 0.35), 0 0 0 0 rgba(255, 209, 102, 0); } }',
      '@media (max-width: 900px) { #op-quests-fab span { display: none; } #op-quests-fab { padding: 10px; } }',
      '@media (prefers-reduced-motion: reduce) { #op-quests-fab { animation: none; } }',
    ].join('\n');
    document.head.appendChild(style);

    var a = document.createElement('a');
    a.id = 'op-quests-fab';
    a.href = '/quests';
    a.title = t('Today\'s surprise is waiting in Quests');

    var img = document.createElement('img');
    img.src = '/static/img/quests/capsule.png?v=' + assetV();
    img.alt = '';
    a.appendChild(img);

    var span = document.createElement('span');
    span.textContent = t('Daily reward');
    var small = document.createElement('small');
    small.textContent = t('tap to open');
    span.appendChild(small);
    a.appendChild(span);

    document.body.appendChild(a);
    watchOverlaysForFab();
  }

  // Fallback for browsers without :has() support (the CSS rule above is the
  // primary mechanism): watch every .overlay's `class` attribute and toggle
  // the FAB's inline display whenever any of them carries `visible`.
  var fabOverlayObserver = null;
  function watchOverlaysForFab() {
    if (fabOverlayObserver || typeof MutationObserver === 'undefined') return;
    function sync() {
      var fab = document.getElementById('op-quests-fab');
      if (!fab) return;
      var anyVisible = Array.prototype.some.call(
        document.querySelectorAll('.overlay'),
        function (o) { return o.classList.contains('visible'); }
      );
      fab.style.display = anyVisible ? 'none' : '';
    }
    fabOverlayObserver = new MutationObserver(sync);
    fabOverlayObserver.observe(document.body, {
      attributes: true, attributeFilter: ['class'], subtree: true,
    });
    sync();
  }

  // ── ADR-17b "Companion" ───────────────────────────────────────────────
  // What /api/quests/badge (Dashboards tab, once a day) or the /quests page
  // said about the creature: {date, seed, since, enabled, autumn} or null.
  // Other workspace pages only READ this — zero requests on their part.
  var COMPANION_KEY = 'op-quests-companion-' + UID;
  function readCompanionCache() {
    try { var raw = localStorage.getItem(COMPANION_KEY); return raw ? JSON.parse(raw) : null; } catch (e) { return null; }
  }
  function writeCompanionCache(info, date) {
    try {
      if (!info) localStorage.removeItem(COMPANION_KEY);
      else localStorage.setItem(COMPANION_KEY, JSON.stringify({
        date: date, seed: info.seed, since: info.since || null,
        enabled: info.enabled !== false, autumn: !!info.autumn,
        checkedAt: Date.now(),
      }));
    } catch (e) {}
  }
  // The pages the creature may visit. Duplicated in quests-companion.js
  // (which cannot be loaded before this check runs): the gate HERE is what
  // keeps /d/{id}, /explore, /admin and share pages at zero extra requests.
  var companionPage = companionPageExport;
  var companionFresh = companionFreshExport;
  var companionLoaded = false;
  function maybeLoadCompanion() {
    if (companionLoaded || ON_QUESTS) return;
    if (!companionPage(location.pathname)) return;
    var info = readCompanionCache();
    if (!info || !info.seed || info.enabled === false) return;
    // A stale entry (yesterday's answer) says nothing about today: an admin
    // switch-off or a «hide» from another device must not linger. The stored
    // date is the SERVER's day (quests run on one timezone), so comparing it
    // with the browser's day alone hid the creature from everyone whose
    // machine is on another timezone, every time the two days differed
    // (found on the stand 2026-09-12, when the server's day rolled first).
    if (!companionFresh(info, localDateStr(), Date.now())) return;
    if (document.body && document.body.getAttribute('data-mode') === 'standalone') return;
    companionLoaded = true;
    window.OP_COMPANION = info;
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/static/css/quests-companion.css?v=' + assetV();
    document.head.appendChild(link);
    var sc = document.createElement('script');
    sc.src = '/static/js/quests-companion.js?v=' + assetV();
    sc.defer = true;
    document.head.appendChild(sc);
  }

  function checkBadge() {
    // Owner decision: the capsule lives on the Dashboards tab only — one
    // place to notice it, no pill trailing the person through every screen.
    if (location.pathname !== '/') { maybeLoadCompanion(); return; }
    var today = localDateStr();
    var cache = readBadgeCache();
    if (badgeFresh(cache, today, Date.now())) {
      if (cache.open === false) mountFab();
      maybeLoadCompanion();
      return;
    }

    fetch('/api/quests/badge', { credentials: 'same-origin' })
      .then(function (res) {
        if (res.status === 404) {
          // Feature disabled server-side — cache a permanent "open" for
          // today so the badge stops asking again until the date rolls.
          writeBadgeCache({ date: today, open: true, checkedAt: Date.now() });
          return null;
        }
        return res.ok ? res.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        var open = !!data.open;
        var entry = { date: data.date || today, open: open };
        if (!open) entry.checkedAt = Date.now();
        writeBadgeCache(entry);
        if (!open) mountFab();
        // ADR-17b: the creature rides the same answer. Remember it for the
        // other workspace pages (they never fetch), then let it in here.
        writeCompanionCache(data.companion || null, data.date || today);
        maybeLoadCompanion();
      })
      .catch(function () { /* silent — no badge, no toast, try again next load */ });
  }

  // The Quests page KNOWS about the companion — its state carries the same block
  // as the badge. So let it write the cache: the "companion unlocked" mark is set
  // exactly here, while on the dashboards list the badge response is cached for 45 minutes, and
  // until the next request the fox did not load — a person collected the eighth
  // card, went to the home page and saw nothing (prod, 16.09). Not a single
  // extra request: we write what already came with the page.
  window.OP_QUESTS_SAVE_COMPANION = function (info, date) {
    try { writeCompanionCache(info || null, date || localDateStr()); } catch (e) {}
  };

  if (ON_QUESTS) {
    // the page renders its own state; no capsule, no badge request here
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', checkBadge);
  } else {
    checkBadge();
  }

  // ── Client-side quest events (theme toggle, grid view) ──────────────
  var inflight = {};
  function sendEvent(kind) {
    var flagKey = 'op-quests-ev-' + kind + '-' + UID;
    try { if (localStorage.getItem(flagKey)) return; } catch (e) {}
    if (inflight[kind]) return; // hook + click can fire together — one request per kind at a time
    inflight[kind] = true;

    fetch('/api/quests/event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: kind }),
      keepalive: true,
      credentials: 'same-origin',
    })
      .then(function (res) {
        // Only remember "already sent" once the server actually accepted
        // it — a network error or a 4xx must not permanently suppress a
        // real quest-eligible action.
        if (!res.ok) return null;
        try { localStorage.setItem(flagKey, '1'); } catch (e) {}
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.award) return;
        if (ON_QUESTS) {
          try { window.dispatchEvent(new CustomEvent('op:quest-award', { detail: data.award })); } catch (e) {}
        } else {
          celebrateHere([data.award]);
        }
      })
      .catch(function () {})
      .then(function () { inflight[kind] = false; });
  }

  // ── Celebrate where the deed was done ─────────────────────────────
  // A festive card in the corner (above the capsule), confetti, and a way
  // straight to the surprise. The quest ids are recorded as «seen» so the
  // /quests page does not throw the same party a second time.
  var SEEN_KEY = 'op-quests-seen-done-' + UID;
  function markSeen(ids) {
    try {
      var raw = localStorage.getItem(SEEN_KEY), seen = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(seen)) seen = [];
      ids.forEach(function (id) { if (seen.indexOf(id) < 0) seen.push(id); });
      localStorage.setItem(SEEN_KEY, JSON.stringify(seen));
    } catch (e) {}
  }
  function celebrateHere(awards) {
    if (!awards || !awards.length) return;
    markSeen(awards.map(function (a) { return a.quest_id; }));
    var pts = 0, surprises = 0;
    awards.forEach(function (a) { pts += a.points || 0; if (a.surprise) surprises++; });
    if (surprises) { writeBadgeCache({ date: localDateStr(), open: false, checkedAt: Date.now() }); mountFab(); }
    var old = document.getElementById('op-quests-cel'); if (old) old.remove();
    var fabEl = document.getElementById('op-quests-fab'); if (fabEl) fabEl.hidden = true; // the card takes the corner
    if (!document.getElementById('op-quests-cel-style')) {
      var st = document.createElement('style'); st.id = 'op-quests-cel-style';
      st.textContent = [
        '#op-quests-cel { position: fixed; right: 20px; bottom: 88px; z-index: 61; width: 320px; padding: 16px 18px;',
        '  border-radius: 16px; color: #fff; background: radial-gradient(ellipse at top left, #24306a, #131a3b 70%);',
        '  box-shadow: 0 20px 50px rgba(10,14,40,.45); font: 13px/1.4 "Inter Tight", system-ui, sans-serif; animation: op-quests-celin .45s cubic-bezier(.2,.8,.2,1); }',
        '#op-quests-cel .e { font: 500 11px "Unbounded", "Inter Tight", sans-serif; letter-spacing: .06em; color: rgba(255,255,255,.65); }',
        '#op-quests-cel .t { font: 700 17px/1.2 "Unbounded", "Inter Tight", sans-serif; margin: 6px 0 10px; }',
        '#op-quests-cel .row { display: flex; gap: 8px; align-items: center; margin: 4px 0; }',
        '#op-quests-cel .ck { width: 22px; height: 22px; border-radius: 50%; background: #2ec4b6; color: #0b1f1d; display: grid; place-items: center; font-weight: 700; flex: none; }',
        '#op-quests-cel .chips { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0 12px; }',
        '#op-quests-cel .chip { padding: 4px 10px; border-radius: 12px; background: rgba(255,255,255,.12); color: #fff; font: 500 12px "Unbounded", "Inter Tight", sans-serif; }',
        '#op-quests-cel .chip b { color: #2ec4b6; font-weight: 700; }',
        '#op-quests-cel .chip.gold { background: #f2c14e; color: #3a2a00; }',
        '#op-quests-cel .acts { display: flex; gap: 8px; align-items: center; }',
        '#op-quests-cel a.go { display: inline-flex; align-items: center; height: 34px; padding: 0 16px; border-radius: 17px; background: #2ec4b6; color: #0b1f1d; text-decoration: none; font: 500 13px "Unbounded", "Inter Tight", sans-serif; }',
        '#op-quests-cel button.x { margin-left: auto; background: none; border: none; color: rgba(255,255,255,.7); font-size: 13px; cursor: pointer; }',
        '#op-quests-cel-cv { position: fixed; inset: 0; pointer-events: none; z-index: 62; }',
        '@keyframes op-quests-celin { from { transform: translateY(16px) scale(.96); opacity: 0; } }',
        '@media (prefers-reduced-motion: reduce) { #op-quests-cel { animation: none; } }',
      ].join('\n');
      document.head.appendChild(st);
    }
    var box = document.createElement('div'); box.id = 'op-quests-cel'; box.setAttribute('role', 'status');
    var e = document.createElement('div'); e.className = 'e'; e.textContent = awards.length === 1 ? t('Task completed') : t('Tasks completed');
    var tv = document.createElement('div'); tv.className = 't'; tv.textContent = surprises ? t('The reward is yours!') : t('Keep it up!');
    box.appendChild(e); box.appendChild(tv);
    awards.forEach(function (a) {
      var row = document.createElement('div'); row.className = 'row';
      var ck = document.createElement('span'); ck.className = 'ck'; ck.textContent = '✓';
      var tt = document.createElement('span'); tt.textContent = a.title || a.quest_id;
      row.appendChild(ck); row.appendChild(tt); box.appendChild(row);
    });
    var chips = document.createElement('div'); chips.className = 'chips';
    var c1 = document.createElement('span'); c1.className = 'chip'; var b1 = document.createElement('b'); b1.textContent = '+' + pts; c1.appendChild(b1); c1.appendChild(document.createTextNode(t(' points'))); chips.appendChild(c1);
    if (surprises) { var c2 = document.createElement('span'); c2.className = 'chip gold'; c2.textContent = '+' + surprises + (surprises === 1 ? t(' surprise') : t(' surprises')); chips.appendChild(c2); }
    box.appendChild(chips);
    var acts = document.createElement('div'); acts.className = 'acts';
    if (surprises) { var go = document.createElement('a'); go.className = 'go'; go.href = '/quests'; go.textContent = t('Open surprise'); acts.appendChild(go); }
    var x = document.createElement('button'); x.className = 'x'; x.type = 'button'; x.textContent = surprises ? t('Later') : t('Great');
    function dismiss() { box.remove(); var cv = document.getElementById('op-quests-cel-cv'); if (cv) cv.remove(); var f = document.getElementById('op-quests-fab'); if (f) f.hidden = false; }
    x.addEventListener('click', dismiss);
    acts.appendChild(x); box.appendChild(acts);
    document.body.appendChild(box);
    confettiBurst(surprises ? 220 : 120);
    setTimeout(function () { if (box.parentNode) dismiss(); }, 12000);
  }
  function confettiBurst(n) {
    var cv = document.getElementById('op-quests-cel-cv');
    if (!cv) { cv = document.createElement('canvas'); cv.id = 'op-quests-cel-cv'; document.body.appendChild(cv); }
    cv.width = window.innerWidth; cv.height = window.innerHeight;
    var cx = cv.getContext('2d'), ps = [], cols = ['#2ec4b6', '#f2c14e', '#ff7b7b', '#7fb6f0', '#ffffff'];
    var x0 = window.innerWidth - 180, y0 = window.innerHeight - 200;
    for (var i = 0; i < n; i++) { var a = -Math.PI / 2 + (Math.random() - .5) * 1.8, v = 6 + Math.random() * 11;
      ps.push({ x: x0, y: y0, vx: Math.cos(a) * v, vy: Math.sin(a) * v, r: Math.random() * Math.PI, vr: (Math.random() - .5) * .3, s: 4 + Math.random() * 6, c: cols[i % cols.length], l: 1, d: .008 + Math.random() * .006 }); }
    (function step() {
      cx.clearRect(0, 0, cv.width, cv.height);
      ps = ps.filter(function (p) { return p.l > 0; });
      ps.forEach(function (p) { p.vy += .28; p.x += p.vx; p.y += p.vy; p.vx *= .985; p.r += p.vr; p.l -= p.d;
        cx.save(); cx.globalAlpha = Math.max(0, Math.min(1, p.l * 1.5)); cx.translate(p.x, p.y); cx.rotate(p.r); cx.fillStyle = p.c; cx.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * .55); cx.restore(); });
      if (ps.length) requestAnimationFrame(step); else cv.remove();
    })();
  }

  // Server-verified quests (favourite, presentation, share link): right after
  // the action's own request succeeds, ask the server to verify and celebrate
  // on the spot instead of waiting for the next /quests visit.
  var VERIFY_AFTER = [
    [/^\/api\/dashboards\/[^/]+\/favorite$/, 'PUT'],
    [/^\/api\/dashboards\/[^/]+\/share-links$/, 'POST'],
    [/^\/api\/dashboards\/[^/]+\/share$/, 'POST'],
    [/^\/api\/presentations$/, 'POST'],
    [/^\/api\/presentations\/[^/]+\/items$/, 'POST'],
  ];
  function shouldVerify(url, method) {
    var path = String(url || '').replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    return VERIFY_AFTER.some(function (r) { return r[0].test(path) && r[1] === String(method || 'GET').toUpperCase(); });
  }
  (function hookFetch() {
    if (typeof window.fetch !== 'function' || window.fetch.__questsHooked) return;
    var orig = window.fetch;
    var wrapped = function (input, init) {
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      var method = (init && init.method) || (input && input.method) || 'GET';
      var p = orig.apply(this, arguments);
      if (shouldVerify(url, method)) {
        p.then(function (res) {
          if (!res || !res.ok) return;
          orig('/api/quests/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d && d.awards && d.awards.length) celebrateHere(d.awards); })
            .catch(function () {});
        }).catch(function () {});
      }
      return p;
    };
    wrapped.__questsHooked = true;
    window.fetch = wrapped;
  })();

  document.addEventListener('click', function (e) {
    var kind = eventKindFor(e.target);
    if (kind && kind !== 'theme') sendEvent(kind); // theme is reported by the OP_UI hook below
  }, true);

  // Theme can be toggled by palette click, palette Enter or a hotkey — all
  // of them go through OP_UI.set('theme', …), so hook that instead of
  // guessing the UI path. Wrapped once; the original is called first.
  (function hookTheme() {
    var ui = window.OP_UI;
    if (!ui || typeof ui.set !== 'function' || ui.__questsHooked) return;
    var orig = ui.set;
    ui.set = function (key, val) {
      var r = orig.apply(this, arguments);
      if (key === 'theme') { try { sendEvent('theme'); } catch (e) {} }
      return r;
    };
    ui.__questsHooked = true;
  })();
})();
