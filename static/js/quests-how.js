/* OnePage — "Show me how" guided overlay engine for /quests.
 *
 * Ported 1:1 in behaviour from the owner-accepted demo (scratchpad/
 * quests-demo-src.html: HOW/showHow/dims/say/playHow), adapted to real
 * OnePage targets:
 *   - `theme`  drives the REAL command palette (components/palette.html +
 *     static/js/palette.js, window.OP_PALETTE) — opened in "inert" mode,
 *     always closed again by howEnd() if this engine opened it.
 *   - `fav` / `grid` / `share` spotlight the real sidebar "Dashboards" nav
 *     item, then hand off to a mini-screen mock (#q-mini) since the actual
 *     dashboards list/report viewer isn't the current page.
 *   - `pres` spotlights the real sidebar "Presentations" nav item, then a
 *     mini-screen mock of the Presentations page.
 *
 * Four fixed dim panels (#q-dimT/B/L/R, computed by dimRects()) frame a
 * rectangular hole around the current target — never a giant box-shadow.
 * #q-spot is the spotlight ring, #q-ghost a moving cursor, #q-callout a
 * speech-bubble label positioned by calloutPos(), #q-keys a centered key
 * combo chip, #q-mini a 460px mock screen for steps that don't have a real
 * on-page target.
 *
 * PURE block (top) is exported for the node harness (tests/quests_how_dom.js)
 * before any DOM is touched — same idiom as static/js/dashboards.js:64-67.
 * Unlike that idiom, `howEnd`/`_timers` are ALSO part of the pure block: every
 * DOM touch inside howEnd()/dims()/say() is guarded by `by()` (which returns
 * null when `document` doesn't exist), so the timer-cleanup contract can be
 * exercised in Node with a stubbed `clearTimeout` and no DOM at all — the
 * file only skips wiring real event listeners (`bindHow()`) when there is no
 * `document`.
 */
(function () {
  'use strict';

  // ═════════════════════════════════════════════════════════════════
  // Pure helpers (exported for node tests — safe with or without a DOM)
  // ═════════════════════════════════════════════════════════════════

  // Four rectangles (top/bottom/left/right) that tile the viewport around
  // `rect` (already padded by the caller) — never a giant box-shadow. Ported
  // from the demo's `dims(r)`. Their combined area always equals
  // `W*H - rect.width*rect.height` when `rect` sits fully inside the
  // viewport (used by the node test as an area-conservation check).
  function dimRects(rect, W, H) {
    const top = Math.max(0, rect.top);
    const bottom = Math.max(0, rect.bottom);
    const left = Math.max(0, rect.left);
    const right = Math.max(0, rect.right);
    return {
      top: { left: 0, top: 0, width: W, height: top },
      bottom: { left: 0, top: bottom, width: W, height: Math.max(0, H - bottom) },
      left: { left: 0, top: top, width: left, height: Math.max(0, bottom - top) },
      right: { left: right, top: top, width: Math.max(0, W - right), height: Math.max(0, bottom - top) },
    };
  }

  // Where the callout speech-bubble goes. Mirrors the demo's inline `say()`
  // call sites: a mini-screen target anchors under the mini card; a real-page
  // target anchors under-right of it; no target (the Ctrl+K keys step)
  // centers near the top third. Always clamped inside the viewport.
  const CALLOUT_W = 320;
  const CALLOUT_H = 80;
  function calloutPos(targetRect, miniRect, W, H) {
    let x, y, center = false;
    if (miniRect) {
      x = miniRect.left;
      y = miniRect.bottom + 10;
    } else if (targetRect) {
      x = targetRect.left + targetRect.width * 0.6 + 16;
      y = targetRect.top + targetRect.height + 14;
    } else {
      x = W / 2;
      y = H * 0.38 + 50;
      center = true;
    }
    const left = Math.max(0, Math.min(x, Math.max(0, W - CALLOUT_W)));
    const top = Math.max(0, Math.min(y, Math.max(0, H - CALLOUT_H)));
    return { left, top, center };
  }

  // ── Mini-screen mocks (static markup only — no user data, no interpolated
  // strings; safe as literal HTML, same trust level as the demo's) ──────
  function mkMini(title, body) {
    return t('<div class="q-mt"><span>Step 2</span>Page: {p1}</div><div class="q-scr">{p2}</div>', {p1: title, p2: body});
  }

  const DASH_LIST =
    t('<div class="q-scr-top"><b>Dashboards</b><span class="q-mk-seg"><em class="on" id="mini-table">Table</em><em id="mini-grid">Grid</em></span></div><div class="q-scr-rows"><div class="q-mk-li"><span>Sales by region</span><i>☆</i></div><div class="q-mk-li hl" id="mini-fav-row"><span>OPS Health · week</span><i id="mini-star">☆</i></div><div class="q-mk-li"><span>Stock levels</span><i>☆</i></div></div>');

  const VIEWER =
    t('<div class="q-scr-top"><b>OPS Health · week</b><span class="q-mk-tabs"><em>Info</em><em>Access</em><em id="mini-share-tab">Share</em></span></div><div class="q-scr-body"><div class="q-scr-chart"></div><div class="q-scr-dock"><div class="q-mk-btn" id="mini-sharebtn">Create link</div><div class="q-mk-hint">Valid 7 days</div></div></div>');

  const PRES =
    t('<div class="q-scr-top"><b>Presentations</b><div class="q-mk-btn" id="mini-new">+ New presentation</div></div><div class="q-scr-rows"><div class="q-mk-li"><span>Weekly summary · 4 slides</span></div><div class="q-mk-li"><span>Director brief · 6 slides</span></div></div>');

  // Result screen for `theme` — built inline in runStep (fxTheme), not
  // mutated from a prior mini, so it's a plain constant here.
  const THEME_RESULT_HTML =
    t('<div class="q-mt"><span>Result</span>Page turns dark</div><div class="q-scr q-scr-dark"><div class="q-scr-top"><b>OnePage</b><span>Dashboards</span></div><div class="q-scr-rows"><div class="q-mk-li"><span>Sales by region</span></div><div class="q-mk-li"><span>OPS Health · week</span></div></div></div>');

  // ── Scenario table — key = `q.how` from app/services/quests.py (pres,
  // fav, theme, grid, share). Each step is one of: {keys,label,ms},
  // {pal,ms}, {sel,label,ms} (optionally with `mini`), {pulse,ms},
  // {click,ms}, {fxTheme,label,ms}, {fx,label,ms}. `sel` targeting the live
  // page is one of the three real selectors below; `sel` targeting content
  // inside the mock screen always starts with "#q-mini ".
  const HOW = {
    theme: [
      { keys: ['Ctrl', 'K'], label: t('Press Ctrl+K — command palette opens'), ms: 1600 },
      { pal: true, ms: 400 },
      { sel: '.palette-row[data-action="toggle:theme"]', label: t('Select «Toggle theme»'), ms: 1800 },
      { pulse: true, ms: 900 },
      { click: true, ms: 700 },
      { fxTheme: true, label: t('Done: palette closes, theme switches to dark. Ctrl+K again returns to light.'), ms: 2600 },
    ],
    fav: [
      { sel: '.nav-item[href="/"]', label: t('Step 1. Open Dashboards'), ms: 1500 },
      { pulse: true, ms: 600 },
      { mini: mkMini('Dashboards', DASH_LIST), sel: '#q-mini #mini-star', label: t('Hover over report row and click star'), ms: 1800 },
      { pulse: true, ms: 900 },
      { click: true, ms: 700 },
      { fx: 'fav', label: t('Done: starred — report moved to Favourites'), ms: 2600 },
    ],
    grid: [
      { sel: '.nav-item[href="/"]', label: t('Step 1. Open Dashboards'), ms: 1500 },
      { pulse: true, ms: 600 },
      { mini: mkMini('Dashboards', DASH_LIST), sel: '#q-mini #mini-grid', label: t('Above the list on the right, switch Table → Grid'), ms: 1800 },
      { pulse: true, ms: 900 },
      { click: true, ms: 700 },
      { fx: 'grid', label: t('Done: rows changed to report preview tiles'), ms: 2600 },
    ],
    share: [
      { sel: '.nav-item[href="/"]', label: t('Step 1. Open your report from Dashboards'), ms: 1500 },
      { pulse: true, ms: 600 },
      { mini: mkMini(t('your report'), VIEWER), sel: '#q-mini #mini-share-tab', label: t('Open Share tab in right panel'), ms: 1600 },
      { sel: '#q-mini #mini-sharebtn', label: t('Click “Create link”'), ms: 1500 },
      { pulse: true, ms: 900 },
      { click: true, ms: 700 },
      { fx: 'share', label: t('Done: link created and copied — ready to share'), ms: 2600 },
    ],
    pres: [
      { sel: '.nav-item[href="/presentations"]', label: t('Step 1. Open “Presentations”'), ms: 1500 },
      { pulse: true, ms: 600 },
      { mini: mkMini(t('Presentations'), PRES), sel: '#q-mini #mini-new', label: t('Click “+ New presentation”'), ms: 1800 },
      { pulse: true, ms: 900 },
      { click: true, ms: 700 },
      { fx: 'pres', label: t('A new presentation will open: add two dashboards to it'), ms: 2600 },
    ],
  };

  // Every scenario step that has a sel/mini/keys/fx/fxTheme must carry a
  // `label`; `ms` must be a positive number; `sel`/`mini` must be strings;
  // `keys` (when present) must be a 2-item array. Returns a list of problem
  // strings — empty means the scenario is well-formed.
  function validateScenario(steps) {
    const problems = [];
    if (!Array.isArray(steps) || !steps.length) {
      problems.push('scenario must be a non-empty array');
      return problems;
    }
    steps.forEach(function (s, i) {
      if (!s || typeof s !== 'object') { problems.push('step ' + i + ': not an object'); return; }
      const needsLabel = !!(s.sel || s.mini || s.keys || s.fx || s.fxTheme);
      if (needsLabel && typeof s.label !== 'string') problems.push('step ' + i + ': missing label');
      if (!(typeof s.ms === 'number' && s.ms > 0)) problems.push('step ' + i + ': ms must be a positive number');
      if (s.sel != null && typeof s.sel !== 'string') problems.push('step ' + i + ': sel must be a string');
      if (s.mini != null && typeof s.mini !== 'string') problems.push('step ' + i + ': mini must be a string');
      if (s.keys != null && !(Array.isArray(s.keys) && s.keys.length === 2)) problems.push('step ' + i + ': keys must be a 2-item array');
    });
    return problems;
  }

  // ── DOM-touching engine — every access goes through by()/qs(), which
  // return null (no-op) when `document` doesn't exist, so this whole block
  // stays safe to construct even under `require()` in Node. ──────────────
  function by(id) { return (typeof document !== 'undefined' && document.getElementById) ? document.getElementById(id) : null; }
  function qs(sel) { return (typeof document !== 'undefined' && document.querySelector) ? document.querySelector(sel) : null; }
  function winW() { return (typeof window !== 'undefined' && window.innerWidth) || 0; }
  function winH() { return (typeof window !== 'undefined' && window.innerHeight) || 0; }

  const timers = [];
  let openedPalette = false;
  let currentKey = null;
  let currentTarget = null;

  function clearAllTimers() {
    timers.forEach(function (tv) { clearTimeout(tv); });
    timers.length = 0; // keep the same array identity — module.exports._timers stays valid
  }

  // ── Accessibility: while a show runs, the underlying page content is
  // aria-hidden (so screen readers don't announce it) and Tab is trapped to
  // the howbar buttons (the only real interactive controls a keyboard user
  // should reach during a show — nothing on the page "for real" is clickable
  // during a demonstration). Both restore on howEnd(). ─────────────────────
  const MAIN_LAYER_IDS = [
    'q-dimT', 'q-dimB', 'q-dimL', 'q-dimR', 'q-spot', 'q-ghost', 'q-callout',
    'q-keys', 'q-mini', 'q-howbar', 'q-how-x',
  ];
  let hiddenMainKids = [];
  function hideMainContent() {
    const root = by('q-root');
    const container = root && root.parentNode;
    if (!container || !container.children) return;
    hiddenMainKids = [];
    Array.prototype.forEach.call(container.children, function (child) {
      if (!child.id || MAIN_LAYER_IDS.indexOf(child.id) !== -1) return;
      if (child.getAttribute('aria-hidden') === 'true') return; // already hidden
      child.setAttribute('aria-hidden', 'true');
      hiddenMainKids.push(child);
    });
  }
  function restoreMainContent() {
    hiddenMainKids.forEach(function (child) { child.removeAttribute('aria-hidden'); });
    hiddenMainKids = [];
  }

  // Registered once (bindHow) rather than added/removed per-show — a no-op
  // whenever the howbar isn't showing.
  function howTabBlocker(e) {
    if (e.key !== 'Tab') return;
    const bar = by('q-howbar');
    if (!bar || bar.hidden) return;
    const active = typeof document !== 'undefined' ? document.activeElement : null;
    const xBtn = by('q-how-x');
    const inAllowed = (bar.contains && active && bar.contains(active)) || active === xBtn;
    if (!inAllowed) e.preventDefault();
  }

  function setPanel(id, r) {
    const e = by(id);
    if (!e) return;
    e.hidden = false;
    e.style.left = r.left + 'px';
    e.style.top = r.top + 'px';
    e.style.width = r.width + 'px';
    e.style.height = r.height + 'px';
  }

  function dims(rect) {
    const r = dimRects(rect, winW(), winH());
    setPanel('q-dimT', r.top);
    setPanel('q-dimB', r.bottom);
    setPanel('q-dimL', r.left);
    setPanel('q-dimR', r.right);
  }

  // `targetRect` XOR `miniRect` — see calloutPos().
  function say(text, targetRect, miniRect) {
    const c = by('q-callout');
    if (!c) return;
    const pos = calloutPos(targetRect || null, miniRect || null, winW(), winH());
    c.hidden = false;
    c.textContent = text;
    c.style.transform = pos.center ? 'translateX(-50%)' : 'none';
    c.style.left = pos.left + 'px';
    c.style.top = pos.top + 'px';
  }

  function hideLayer(id) {
    const e = by(id);
    if (e) e.hidden = true;
  }

  // Clears every pending step timer, hides every layer, closes the palette
  // if this engine opened it, and removes any leftover press/ripple state.
  // Bound to: #q-how-x, #q-how-ok, Escape, click on any dim panel,
  // beforeunload, visibilitychange -> hidden, and window resize.
  function howEnd() {
    clearAllTimers();
    ['q-spot', 'q-ghost', 'q-callout', 'q-keys', 'q-mini', 'q-howbar', 'q-how-x',
      'q-dimT', 'q-dimB', 'q-dimL', 'q-dimR'].forEach(hideLayer);
    restoreMainContent();
    const spot = by('q-spot');
    if (spot && spot.classList) spot.classList.remove('q-pulse');
    const ghost = by('q-ghost');
    if (ghost && ghost.classList) ghost.classList.remove('q-press');
    if (openedPalette && typeof window !== 'undefined' && window.OP_PALETTE) {
      window.OP_PALETTE.close();
    }
    openedPalette = false;
    currentTarget = null;
    if (typeof document !== 'undefined' && document.querySelectorAll) {
      document.querySelectorAll('.q-pressed').forEach(function (e) { e.classList.remove('q-pressed'); });
      document.querySelectorAll('.q-ripple').forEach(function (e) { if (e.remove) e.remove(); });
    }
  }

  function runStep(s) {
    if (s.keys) {
      dims({ top: winH() / 2, bottom: winH() / 2, left: winW() / 2, right: winW() / 2 });
      const k = by('q-keys');
      if (k) {
        k.hidden = false;
        while (k.firstChild) k.removeChild(k.firstChild);
        const kbd1 = document.createElement('kbd');
        kbd1.textContent = s.keys[0];
        k.appendChild(kbd1);
        k.appendChild(document.createTextNode('+'));
        const kbd2 = document.createElement('kbd');
        kbd2.textContent = s.keys[1];
        k.appendChild(kbd2);
      }
      const spot = by('q-spot');
      if (spot) spot.hidden = true; // no target yet — a 0×0 ring would show as a dot
      say(s.label, null, null);
    }

    if (s.pal) {
      hideLayer('q-keys');
      if (typeof window !== 'undefined' && window.OP_PALETTE) {
        window.OP_PALETTE.open();
        openedPalette = true;
        const input = by('palette-input');
        if (input && input.blur) input.blur(); // don't let the palette steal focus during the show
      }
    }

    if (s.mini) {
      const m = by('q-mini');
      if (m) {
        m.hidden = false;
        m.innerHTML = s.mini; // static, hardcoded markup only — see mkMini() above
        // Anchor next to the last spotlit element (the sidebar item), like the demo:
        // to its right, roughly level with it, always inside the viewport.
        const W = winW(), H = winH();
        const r0 = currentTarget && currentTarget.getBoundingClientRect
          ? currentTarget.getBoundingClientRect() : { right: 240, top: 120 };
        m.style.left = Math.max(12, Math.min(r0.right + 28, W - 480)) + 'px';
        m.style.top = Math.max(60, Math.min(r0.top - 40, H - 340)) + 'px';
      }
    }

    if (s.sel) {
      const e = qs(s.sel);
      if (e) {
        currentTarget = e;
        const insideMini = !!(e.closest && e.closest('#q-mini'));
        if (!insideMini && e.scrollIntoView) e.scrollIntoView({ block: 'nearest' });
        {
          // Re-measure once entrance animations (mini-screen, command palette)
          // have settled so the ring hugs the target, not its mid-animation
          // position.
          timers.push(setTimeout(function () {
            const r2 = e.getBoundingClientRect(), sp = by('q-spot'), gh = by('q-ghost');
            if (sp && !sp.hidden) { sp.style.left = (r2.left - 6) + 'px'; sp.style.top = (r2.top - 6) + 'px'; sp.style.width = (r2.width + 12) + 'px'; sp.style.height = (r2.height + 12) + 'px'; }
            if (gh && !gh.hidden) { gh.style.left = (r2.left + r2.width * 0.6) + 'px'; gh.style.top = (r2.top + r2.height * 0.6) + 'px'; }
          }, 320));
        }
        const r = e.getBoundingClientRect();
        const spot = by('q-spot');
        if (spot) {
          spot.hidden = false;
          if (spot.classList) spot.classList.remove('q-pulse');
          spot.style.left = (r.left - 6) + 'px';
          spot.style.top = (r.top - 6) + 'px';
          spot.style.width = (r.width + 12) + 'px';
          spot.style.height = (r.height + 12) + 'px';
        }
        dims({ top: r.top - 6, bottom: r.bottom + 6, left: r.left - 6, right: r.right + 6 });
        const ghost = by('q-ghost');
        if (ghost) {
          ghost.style.left = (r.left + r.width * 0.6) + 'px';
          ghost.style.top = (r.top + r.height * 0.6) + 'px';
        }
        const mini = by('q-mini');
        const inMini = !!(e.closest && mini && e.closest('#q-mini'));
        if (inMini) {
          const mr = mini.getBoundingClientRect();
          say(s.label, null, mr);
        } else {
          say(s.label, r, null);
        }
      }
      // else: target missing (e.g. sidebar hidden on a narrow screen) — skip
      // this step's spotlight/dim/callout gracefully; later steps (the mini
      // screen in particular) still run normally.
    }

    if (s.pulse) {
      const spot = by('q-spot');
      if (spot && spot.classList) spot.classList.add('q-pulse');
    }

    if (s.click) {
      if (currentTarget) {
        const rc = currentTarget.getBoundingClientRect();
        if (typeof document !== 'undefined' && document.createElement && document.body) {
          const rp = document.createElement('div');
          rp.className = 'q-ripple';
          rp.style.left = (rc.left + rc.width * 0.6) + 'px';
          rp.style.top = (rc.top + rc.height * 0.6) + 'px';
          document.body.appendChild(rp);
          timers.push(setTimeout(function () { if (rp.remove) rp.remove(); }, 700));
        }
        if (currentTarget.classList) {
          currentTarget.classList.add('q-pressed');
          const tgt = currentTarget;
          timers.push(setTimeout(function () { tgt.classList.remove('q-pressed'); }, 300));
        }
      }
      const ghost = by('q-ghost');
      if (ghost && ghost.classList) {
        ghost.classList.add('q-press');
        timers.push(setTimeout(function () { ghost.classList.remove('q-press'); }, 250));
      }
      hideLayer('q-callout');
    }

    if (s.fxTheme) {
      if (openedPalette && typeof window !== 'undefined' && window.OP_PALETTE) {
        window.OP_PALETTE.close();
        openedPalette = false;
      }
      hideLayer('q-spot');
      hideLayer('q-ghost');
      dims({ top: winH() / 2, bottom: winH() / 2, left: winW() / 2, right: winW() / 2 });
      const mm = by('q-mini');
      if (mm) {
        mm.hidden = false;
        mm.style.left = (winW() / 2 - 220) + 'px';
        mm.style.top = '120px';
        mm.innerHTML = THEME_RESULT_HTML;
        const mr = mm.getBoundingClientRect();
        dims({ top: mr.top - 8, bottom: mr.bottom + 8, left: mr.left - 8, right: mr.right + 8 });
        say(s.label, null, mr);
      }
    }

    if (s.fx) {
      const m = by('q-mini');
      if (m && m.querySelector) {
        const eyebrow = m.querySelector('.q-mt span');
        if (eyebrow) eyebrow.textContent = t('Result {ctx:2}');
        hideLayer('q-spot');
        applyFx(m, s.fx);
        const mr = m.getBoundingClientRect();
        dims({ top: mr.top - 8, bottom: mr.bottom + 8, left: mr.left - 8, right: mr.right + 8 });
        const ghost = by('q-ghost');
        if (ghost) {
          ghost.style.left = (mr.right - 30) + 'px';
          ghost.style.top = (mr.bottom - 30) + 'px';
        }
        say(s.label, null, mr);
      }
    }
  }

  // Mutates the mock screen currently in #q-mini to show the "after" state
  // — matches the demo's `fx` branch 1:1 (grid tiles / fav star / share
  // link / new presentation with two empty slides). All markup is static.
  function applyFx(mini, kind) {
    if (kind === 'grid') {
      const rows = mini.querySelector('.q-scr-rows');
      if (rows) {
        rows.outerHTML =
          t('<div class="q-scr-grid"><div class="q-gc"><i class="q-gc-i q-gc-1"></i><b>Sales by region</b></div><div class="q-gc"><i class="q-gc-i q-gc-2"></i><b>OPS Health · week</b></div><div class="q-gc"><i class="q-gc-i q-gc-3"></i><b>Stock levels</b></div></div>');
      }
    } else if (kind === 'fav') {
      const rows = mini.querySelector('.q-scr-rows');
      const hl = mini.querySelector('.q-mk-li.hl');
      if (rows && hl) {
        const star = hl.querySelector('i');
        if (star) { star.textContent = '★'; star.classList.add('q-star-on'); }
        const sec = document.createElement('div');
        sec.className = 'q-scr-sec';
        sec.textContent = t('★ Favorites');
        rows.insertBefore(sec, rows.firstChild);
        rows.insertBefore(hl, rows.children[1]);
        const rest = document.createElement('div');
        rest.className = 'q-scr-sec';
        rest.textContent = t('Other');
        rows.insertBefore(rest, rows.children[2]);
      }
    } else if (kind === 'share') {
      const dock = mini.querySelector('.q-scr-dock');
      if (dock) {
        dock.innerHTML =
          t('<div class="q-mk-hint">Link created</div><div class="q-scr-link">https://onepage/…/s/k7Qm2x</div><div class="q-scr-ok">Copied</div>');
      }
    } else if (kind === 'pres') {
      const title = mini.querySelector('.q-scr-top b');
      if (title) title.textContent = t('New presentation · 0 slides');
      const newBtn = mini.querySelector('#mini-new');
      if (newBtn) newBtn.outerHTML = t('<div class="q-mk-btn">+ Add dashboard</div>');
      const rows = mini.querySelector('.q-scr-rows');
      if (rows) {
        rows.innerHTML =
          t('<div class="q-scr-slide">Slide 1: select dashboard</div><div class="q-scr-slide">Slide 2: select dashboard</div>');
      }
    }
  }

  function showHow(key) {
    const steps = HOW[key];
    if (!steps) return;
    currentKey = key;
    howEnd();
    hideMainContent();
    const bar = by('q-howbar');
    if (bar) bar.hidden = false;
    const x = by('q-how-x');
    if (x) x.hidden = false;
    const ghost = by('q-ghost');
    if (ghost) {
      ghost.hidden = false;
      ghost.style.left = '40%';
      ghost.style.top = '60%';
    }
    let tv = 200;
    steps.forEach(function (s) {
      timers.push(setTimeout(function () { runStep(s); }, tv));
      tv += (s.ms || 1000);
    });
  }

  function bindHow() {
    const panel = by('q-quests');
    if (panel) {
      panel.addEventListener('click', function (e) {
        const b = e.target.closest && e.target.closest('[data-how]');
        if (b) showHow(b.getAttribute('data-how'));
      });
    }
    const okBtn = by('q-how-ok');
    if (okBtn) okBtn.addEventListener('click', howEnd);
    const xBtn = by('q-how-x');
    if (xBtn) xBtn.addEventListener('click', howEnd);
    const againBtn = by('q-how-again');
    if (againBtn) againBtn.addEventListener('click', function () { if (currentKey) showHow(currentKey); });
    ['q-dimT', 'q-dimB', 'q-dimL', 'q-dimR'].forEach(function (id) {
      const e = by(id);
      if (e) e.addEventListener('click', howEnd);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        const bar = by('q-howbar');
        if (bar && !bar.hidden) howEnd();
      }
    });
    document.addEventListener('keydown', howTabBlocker);
    if (typeof window !== 'undefined') {
      window.addEventListener('beforeunload', howEnd);
      window.addEventListener('resize', howEnd); // re-end rather than reposition on stale geometry
    }
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) howEnd();
    });
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      HOW: HOW,
      dimRects: dimRects,
      calloutPos: calloutPos,
      validateScenario: validateScenario,
      howEnd: howEnd,
      _timers: timers,
      showHow: showHow,
      howTabBlocker: howTabBlocker,
    };
  }
  if (typeof document === 'undefined') return; // node require() path stops here — no real DOM to wire up

  bindHow();
})();
