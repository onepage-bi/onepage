/* OnePage — viewer.js
 * Preserves the existing postMessage protocol (op_ready / op_file / op_save_file)
 * and adds dock-tab UI behavior.
 *
 * Globals from template: DASHBOARD_ID, CAN_UPDATE_DATA, CAN_MANAGE, SLOTS, IS_PUBLIC
 */

(function() {

  // ── "Chat about the report": pure helpers ───────────────────────────
  // Everything above the `typeof document` bail-out below is pure, so
  // tests/viewer_chat_dom.js can require this file in node and drive the
  // pane over a fake DOM (same idiom as static/js/presentations.js).
  //
  // The pane is a COLUMN of the .viewer grid: it pushes the report aside
  // instead of covering it. Below 760px there is no width to give away, so
  // the same markup becomes a full-width overlay (see main.css) and the grip
  // disappears.
  const CHAT_MIN_W = 340;    // narrower than this the composer wraps
  const CHAT_DEF_W = 440;    // the default, and what a double-click resets to
  const CHAT_MAX_VW = 0.62;  // the report keeps at least the remaining 38%
  const CHAT_WIDE_VW = 0.50; // "wider" = half the window
  const REPORT_MIN_W = 560;  // the floor the 2026-08-23 complaint was about

  // The ceiling is arithmetic, not CSS: the chat may take 62% of the width,
  // but never so much that the report drops below 560px — that squeeze is
  // exactly what got the column removed in 2026-08-23. `avail` is the width
  // the .viewer itself has (NOT the window: the sidebar is outside it) and
  // `dock` is what the dock column is holding right now, so closing the dock
  // really does give the chat more room.
  function chatMaxW(avail, dock) {
    const total = Number(avail) || 0;
    const room = total - (Number(dock) || 0) - REPORT_MIN_W;
    return Math.max(CHAT_MIN_W, Math.min(Math.floor(total * CHAT_MAX_VW), Math.floor(room)));
  }
  function chatClampW(w, avail, dock) {
    const max = chatMaxW(avail, dock);
    const width = Number(w);
    if (!isFinite(width)) return Math.min(CHAT_DEF_W, max);
    return Math.max(CHAT_MIN_W, Math.min(max, Math.round(width)));
  }
  function chatWideW(avail, dock) {
    return chatClampW(Math.round((Number(avail) || 0) * CHAT_WIDE_VW), avail, dock);
  }
  function chatPaneW(st, avail, dock) {
    if (!st || !st.open) return 0;
    return st.wide ? chatWideW(avail, dock) : chatClampW(st.w, avail, dock);
  }

  // Persisted state. The old format was one string ('open' | 'only' |
  // 'closed') — a browser that has it must not lose its open pane, so it is
  // read as an open pane of default width.
  function chatReadState(raw) {
    const st = { open: false, w: CHAT_DEF_W, wide: false };
    if (raw == null) return st;
    const s = String(raw);
    if (s === 'open' || s === 'only') { st.open = true; return st; }
    if (s === 'closed') return st;
    let o = null;
    try { o = JSON.parse(s); } catch (e) { o = null; }
    if (!o || typeof o !== 'object') return st;
    st.open = o.open === true;
    st.wide = o.wide === true;
    const w = Number(o.w);
    if (isFinite(w) && w > 0) st.w = Math.round(w);
    return st;
  }
  function chatWriteState(st) {
    return JSON.stringify({ open: !!st.open, w: Math.round(Number(st.w) || CHAT_DEF_W), wide: !!st.wide });
  }

  // Same rule as hotkeys.js / presentations.js: a shortcut never fires while
  // someone is typing.
  function chatIsTyping(el) {
    if (!el) return false;
    const tag = (el.tagName || '').toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (el.isContentEditable) return true;
    const ce = el.getAttribute && el.getAttribute('contenteditable');
    return ce === '' || ce === 'true';
  }

  // Who owns Escape. Three owners already exist on this page — present mode
  // (viewer.js), show mode (presentations.js) and any open menu — so the chat
  // takes the key only when nobody else has:
  //   • a handler that already ran and called preventDefault wins;
  //   • present / fullscreen keeps Esc (the pane stays visible mid-show and is
  //     closed with its own button);
  //   • focus in a field, or in the chat frame itself, means the field wins —
  //     the frame is same-origin and posts {type:'op_chat_close'} when ITS
  //     composer is empty, which is the path that closes the pane from inside.
  function chatEscCloses(e, ctx) {
    if (!e || e.key !== 'Escape' || e.defaultPrevented) return false;
    if (!ctx || !ctx.open) return false;
    if (ctx.present || ctx.fullscreen) return false;
    if (chatIsTyping(ctx.active)) return false;
    if (ctx.active && (ctx.active.tagName || '').toUpperCase() === 'IFRAME') return false;
    return true;
  }

  // "Wider / narrower" also on ⌘/Ctrl+Shift+] — the same key as the prototype.
  function chatWideKey(e) {
    if (!e || e.defaultPrevented || !e.shiftKey) return false;
    if (!(e.metaKey || e.ctrlKey)) return false;
    return e.key === ']' || e.key === '}' || e.code === 'BracketRight';
  }

  // The pane itself. `env` is everything it touches: {doc, win, storage,
  // reportId}. Nothing here reads a global, so node can mount it over a fake
  // document.
  function mountChatPane(env) {
    const doc = env.doc;
    const win = env.win;
    const pane = doc.getElementById('discuss-pane');
    const fab = doc.getElementById('discuss-fab');
    if (!pane || !fab) return null;   // not a discussable report

    const root = doc.getElementById('viewer-root');
    const body = doc.getElementById('discuss-body') || pane;
    const grip = doc.getElementById('discuss-grip');
    const countEl = doc.getElementById('discuss-count');
    const wideBtn = doc.getElementById('discuss-wide');
    const closeBtn = doc.getElementById('discuss-close');
    const reportId = env.reportId;
    const store = env.storage || null;
    const LS_KEY = 'op-viewer-chat:' + reportId;
    const SRC = '/explore?report=' + encodeURIComponent(reportId) + '&mode=chat&embed=1';
    const label = t('Report chat');   // i18n.js is loaded before viewer.js

    let dropTimer = null;
    let pending = null;     // frame creation waiting for the report's first frame
    let reportReady = false;
    let drag = null;

    function readRaw() { try { return store ? store.getItem(LS_KEY) : null; } catch (e) { return null; } }
    function save() { try { if (store) store.setItem(LS_KEY, chatWriteState(st)); } catch (e) { /* private mode */ } }
    const st = chatReadState(readRaw());

    // The space the pane competes for is the .viewer's, and the dock is its
    // rival for it. A fake DOM has no boxes; there the window is the answer.
    function boxW(el) {
      if (!el || typeof el.getBoundingClientRect !== 'function') return 0;
      try { return Math.round(el.getBoundingClientRect().width) || 0; } catch (e) { return 0; }
    }
    function vw() { return boxW(root) || Number(win.innerWidth) || 1280; }
    function dockW() {
      const d = doc.querySelector ? doc.querySelector('.v-dock') : null;
      return (root && root.getAttribute('data-dock') === 'closed') ? 0 : boxW(d);
    }
    function width() { return chatPaneW(st, vw(), dockW()); }
    function frameEl() { return doc.getElementById('discuss-frame'); }

    function postWidth() {
      const f = frameEl();
      if (!f || !f.contentWindow) return;
      const origin = (win.location && win.location.origin) || '*';
      try { f.contentWindow.postMessage({ type: 'op_chat_width', w: width(), wide: !!st.wide }, origin); } catch (e) { /* frame not up yet */ }
    }
    function ensureFrame() {
      let f = frameEl();
      if (f) return f;
      f = doc.createElement('iframe');
      f.id = 'discuss-frame';
      f.className = 'v-chat-frame';
      f.title = label;
      f.src = SRC;
      if (f.addEventListener) f.addEventListener('load', postWidth);
      body.appendChild(f);
      return f;
    }
    function dropFrame() { const f = frameEl(); if (f) f.remove(); }

    // Hot path invariant: opening /d/{id} with a remembered open pane costs
    // ZERO new requests while the report loads. The grid is already in its
    // open shape (that is only CSS), and the /explore frame is created after
    // the report's first frame — op_ready, or an idle callback if a report
    // never says it is ready.
    function whenIdle(fn) {
      if (reportReady) { fn(); return; }
      pending = fn;
      const ric = win.requestIdleCallback;
      if (typeof ric === 'function') ric(runPending, { timeout: 4000 });
      else if (win.setTimeout) win.setTimeout(runPending, 1200);
    }
    function runPending() {
      const fn = pending;
      pending = null;
      if (fn) fn();
    }

    function apply(persist) {
      const w = width();
      if (root) {
        if (root.style && root.style.setProperty) root.style.setProperty('--chat-w', w + 'px');
        root.setAttribute('data-chat', st.open ? 'open' : 'closed');
      }
      pane.hidden = !st.open;
      fab.setAttribute('aria-expanded', st.open ? 'true' : 'false');
      if (wideBtn) wideBtn.setAttribute('aria-pressed', st.wide ? 'true' : 'false');
      if (persist !== false) save();
      if (st.open) postWidth();
    }

    // Closing HIDES the pane but keeps its frame: dropping it threw away a
    // built index, and every return to the chat rebuilt it for ~30 s (prod
    // 2026-08-23). The worker is unloaded only after 10 quiet minutes.
    function setOpen(open, opts) {
      const o = opts || {};
      const was = st.open;
      st.open = !!open;
      if (win.clearTimeout) win.clearTimeout(dropTimer);
      if (st.open) {
        if (o.defer) whenIdle(ensureFrame); else ensureFrame();
      } else if (win.setTimeout) {
        dropTimer = win.setTimeout(dropFrame, 10 * 60 * 1000);
      }
      apply(o.persist);
      if (was && !st.open && o.focus !== false && fab.focus) fab.focus();
    }
    function setWide(on, persist) {
      st.wide = (on === undefined) ? !st.wide : !!on;
      apply(persist);
    }
    function setWidth(w, persist) {
      st.wide = false;
      st.w = chatClampW(w, vw(), dockW());
      apply(persist);
    }

    fab.addEventListener('click', () => setOpen(!st.open));
    if (closeBtn) closeBtn.addEventListener('click', () => setOpen(false));
    if (wideBtn) wideBtn.addEventListener('click', () => setWide());

    doc.addEventListener('keydown', (e) => {
      if (chatWideKey(e)) { if (e.preventDefault) e.preventDefault(); setWide(); return; }
      const ctx = {
        open: st.open,
        active: doc.activeElement,
        present: !!(doc.body && doc.body.classList && doc.body.classList.contains('op-present')),
        fullscreen: !!doc.fullscreenElement,
      };
      if (!chatEscCloses(e, ctx)) return;
      if (e.preventDefault) e.preventDefault();
      setOpen(false);
    });

    // The frame boundary. Same-origin check first, then a fixed vocabulary:
    // a count (a number), "close me" and "wider". Nothing else is read.
    win.addEventListener('message', (e) => {
      if (win.location && e.origin !== win.location.origin) return;
      const d = e.data;
      if (!d) return;
      if (d.type === 'op_chat_count') {
        // The embedded page has its own header (title, coverage pill, «⋯»,
        // wide, close) — once it has spoken, the pane's fallback header only
        // duplicates it (two "Close", two "wider" — stand 2026-09-07).
        const head = pane && pane.querySelector ? pane.querySelector('.v-chat-head') : null;
        if (head) head.hidden = true;
        if (!countEl) return;
        const n = Number(d.n);
        const v = (isFinite(n) && n >= 0) ? Math.floor(n) : 0;
        countEl.textContent = String(v);
        countEl.hidden = v === 0;
        return;
      }
      if (d.type === 'op_chat_close') { setOpen(false); return; }
      if (d.type === 'op_chat_wide') { setWide(d.wide === undefined ? undefined : !!d.wide); return; }
    });

    if (grip) {
      grip.addEventListener('pointerdown', (e) => {
        drag = { x: e.clientX, w: width() };
        st.wide = false;
        if (root && root.classList) root.classList.add('is-chat-resizing');
        try { grip.setPointerCapture(e.pointerId); } catch (e2) { /* no capture, still works */ }
        if (e.preventDefault) e.preventDefault();
      });
      grip.addEventListener('pointermove', (e) => {
        if (!drag) return;
        st.w = chatClampW(drag.w + (drag.x - e.clientX), vw(), dockW());
        apply(false);
      });
      const endDrag = (e) => {
        if (!drag) return;
        drag = null;
        if (root && root.classList) root.classList.remove('is-chat-resizing');
        try { grip.releasePointerCapture(e.pointerId); } catch (e2) { /* ignore */ }
        apply();
      };
      grip.addEventListener('pointerup', endDrag);
      grip.addEventListener('pointercancel', endDrag);
      grip.addEventListener('dblclick', () => { st.w = CHAT_DEF_W; st.wide = false; apply(); });
    }

    win.addEventListener('resize', () => apply(false));

    // First paint: the remembered geometry, no frame yet.
    apply(false);
    if (st.open) whenIdle(ensureFrame);

    return {
      setOpen: setOpen,
      setWide: setWide,
      setWidth: setWidth,
      width: width,
      state: () => ({ open: st.open, w: st.w, wide: st.wide }),
      // The dock is the pane's rival for the width — opening or closing it
      // re-runs the arithmetic.
      reflow: () => apply(false),
      hasFrame: () => !!frameEl(),
      // Called from the op_ready branch of the #dash-frame bridge.
      onReportReady: () => { reportReady = true; runPending(); },
    };
  }

  // ── Navigator: what to do with a link the page asked us to open ─────
  // The rules live in page-links.js: TWO shells apply them, this viewer and
  // the public share page, and they must not drift apart. Only the binding is
  // here: in the browser the module is loaded before viewer.js, under node
  // it is required.
  var PAGE_LINKS = (typeof module !== 'undefined' && module.exports)
    ? require('./page-links.js')
    : (typeof window !== 'undefined' ? window.OP_PAGE_LINKS : null);
  var pageLinkAction = PAGE_LINKS ? PAGE_LINKS.pageLinkAction : null;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      CHAT_MIN_W: CHAT_MIN_W, CHAT_DEF_W: CHAT_DEF_W,
      chatClampW: chatClampW, chatWideW: chatWideW, chatPaneW: chatPaneW,
      chatMaxW: chatMaxW, REPORT_MIN_W: REPORT_MIN_W,
      chatReadState: chatReadState, chatWriteState: chatWriteState,
      chatIsTyping: chatIsTyping, chatEscCloses: chatEscCloses,
      chatWideKey: chatWideKey, mountChatPane: mountChatPane,
      pageLinkAction: pageLinkAction,
    };
  }
  if (typeof document === 'undefined') return;  // node require() path stops here


  // ── Clipboard helper (works on HTTP too) ────────────────────────────
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  // ── Dock tabs ───────────────────────────────────────────────────────
  document.querySelectorAll('.dock-tab').forEach(tv => {
    tv.addEventListener('click', () => {
      document.querySelectorAll('.dock-tab').forEach(x => x.classList.remove('active'));
      document.querySelectorAll('.dock-panel').forEach(x => x.classList.remove('active'));
      tv.classList.add('active');
      document.querySelector(`.dock-panel[data-tab="${tv.dataset.tab}"]`)?.classList.add('active');
      // Annotate picking follows the active tab.
      if (typeof ANNOT !== 'undefined' && ANNOT) ANNOT.setPicking(tv.dataset.tab === 'annotate');
      if (typeof THEME !== 'undefined' && THEME) THEME.onTab(tv.dataset.tab);
      if (typeof TEXT !== 'undefined' && TEXT) TEXT.onTab(tv.dataset.tab);
    });
  });

  // ── Dock toggle ─────────────────────────────────────────────────────
  document.getElementById('dock-toggle')?.addEventListener('click', () => {
    const root = document.getElementById('viewer-root');
    const open = root.getAttribute('data-dock') !== 'open';
    root.setAttribute('data-dock', open ? 'open' : 'closed');
    if (typeof ANNOT !== 'undefined' && ANNOT) {
      const annotActive = document.querySelector('.dock-tab[data-tab="annotate"]')?.classList.contains('active');
      ANNOT.setPicking(open && !!annotActive);
    }
    if (typeof THEME !== 'undefined' && THEME && !open) THEME.stopPick();
    if (typeof TEXT !== 'undefined' && TEXT && !open) TEXT.stopEdit();
    // The chat column competes with the dock for the same width.
    try { if (CHAT) setTimeout(CHAT.reflow, 300); } catch (e) { /* no chat here */ }
  });

  // ── "Chat about the report" pane ────────────────────────────────────
  // The chat beside a report is /explore itself in a SAME-ORIGIN iframe
  // (`/explore?report=…&mode=chat&embed=1`) — same origin, same IndexedDB,
  // therefore literally the same thread as the /explore page. Nothing about
  // #dash-frame is touched: that iframe keeps its sandbox and its protocol.
  const CHAT = mountChatPane({
    doc: document,
    win: window,
    storage: (function () { try { return window.localStorage; } catch (e) { return null; } })(),
    reportId: DASHBOARD_ID,
  });

  // ── "Show in report": relay chat frame → report frame (P2) ──────────
  // The chat cannot touch the report: #dash-frame is sandboxed without
  // allow-same-origin, so its window is unreachable from the chat's own
  // (same-origin) frame. The parent is the only thing that can speak to both,
  // and it speaks a fixed vocabulary: one command down, one scalar answer up.
  // If the report never answers (no controller, torn-down frame, a report that
  // swallowed the message), the 1.5 s timeout answers instead — the button
  // must say "not found", never wait forever or claim a highlight that is not there.
  (function chatLocateRelay() {
    const dash = document.getElementById('dash-frame');
    const pane = document.getElementById('discuss-pane');
    if (!dash || !pane) return;

    const pending = new Map();          // request id → timeout handle
    const str = (v, n) => (typeof v === 'string' || typeof v === 'number')
      ? String(v).slice(0, n || 200) : '';

    function chatWin() {
      const f = document.getElementById('discuss-frame');
      return f ? f.contentWindow : null;
    }
    function answer(id, ok, tag, text, reason) {
      if (!pending.has(id)) return;     // already answered, or never asked
      clearTimeout(pending.get(id));
      pending.delete(id);
      const w = chatWin();
      if (!w) return;
      try {
        w.postMessage({ type: 'op_chat_located', id, ok, tag, text, reason },
                      location.origin);
      } catch (e) { /* frame went away mid-flight */ }
    }

    // chat frame → parent
    window.addEventListener('message', (e) => {
      if (e.origin !== location.origin) return;
      const w = chatWin();
      if (!w || e.source !== w) return;
      const d = e.data;
      if (!d || d.type !== 'op_chat_locate') return;
      const id = str(d.id, 64) || 'loc' + Date.now();
      if (pending.has(id)) return;
      const labels = Array.isArray(d.labels)
        ? d.labels.map(x => str(x)).filter(Boolean).slice(0, 20) : [];
      pending.set(id, setTimeout(() => answer(id, false, '', '', 'timeout'), 1500));
      // Same channel and same targetOrigin as the annotate protocol: the
      // report frame's origin is opaque, so '*' is the only value it matches.
      try {
        dash.contentWindow.postMessage({
          type: 'op_locate', id,
          q: str(d.q), kpi: str(d.kpi), value: str(d.value), labels,
        }, '*');
      } catch (err) { answer(id, false, '', '', 'timeout'); }
    });

    // report frame → parent
    window.addEventListener('message', (e) => {
      if (e.source !== dash.contentWindow) return;
      const d = e.data;
      if (!d || d.type !== 'op_located') return;
      answer(str(d.id, 64), !!d.ok, str(d.tag, 32), str(d.text, 60),
             d.ok ? '' : 'not_found');
    });
  })();

  // ── Copy link button ────────────────────────────────────────────────
  document.getElementById('btn-share-link')?.addEventListener('click', async () => {
    const url = `${location.origin}/d/${DASHBOARD_ID}`;
    try {
      await copyText(url);
      window.OP_TOAST('Link copied', 'success');
    } catch (e) {
      window.OP_TOAST('Could not copy', 'error');
    }
  });

  // ── Present mode (In this tab / Fullscreen / New tab) ───────────────
  (function presentMode() {
    const btn = document.getElementById('btn-present');
    const menu = document.getElementById('present-menu');
    const exitBtn = document.getElementById('present-exit');
    if (!btn || !menu) return;

    function openMenu(open) {
      menu.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function isPresent() { return document.body.classList.contains('op-present'); }
    // Show mode (`?pres=…`) reuses op-present's chrome-hiding rules, but its
    // Esc belongs to the presenter bar (→ back to the builder), not to this
    // menu — presentations.js owns that key while op-pres is on.
    function inShow() { return document.body.classList.contains('op-pres'); }
    function enterPresent() { document.body.classList.add('op-present'); }
    function exitPresent() {
      document.body.classList.remove('op-present');
      if (document.fullscreenElement) document.exitFullscreen?.();
    }
    function enterFullscreen() {
      enterPresent();
      // The WHOLE document goes fullscreen, not .v-main: op-present already
      // hides the chrome, and the fixed chat widget must stay reachable
      // mid-show (owner 2026-08-23) — an element-scoped fullscreen would
      // clip out everything fixed outside .v-main.
      try { document.documentElement.requestFullscreen?.().catch(() => {}); } catch (e) { /* present still applied */ }
    }

    btn.addEventListener('click', e => { e.stopPropagation(); openMenu(menu.hidden); });
    document.addEventListener('click', e => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) openMenu(false);
    });
    menu.addEventListener('click', e => {
      const item = e.target.closest('[data-present]');
      if (!item) return;
      openMenu(false);
      const mode = item.dataset.present;
      if (mode === 'tab') enterPresent();
      else if (mode === 'fullscreen') enterFullscreen();
      else if (mode === 'newtab') window.open(`/d/${DASHBOARD_ID}?present=1`, '_blank');
    });

    exitBtn?.addEventListener('click', exitPresent);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && isPresent() && !inShow() && !document.fullscreenElement) exitPresent();
    });
    // Leaving fullscreen (browser Esc) drops back to normal chrome.
    document.addEventListener('fullscreenchange', () => {
      if (!document.fullscreenElement && isPresent()) document.body.classList.remove('op-present');
    });

    // Auto-enter when opened via "New tab" (?present=1).
    if (new URLSearchParams(location.search).get('present') === '1') enterPresent();
    // Navigator: "Page only" is the same mode as Present → In this tab.
    // The mode itself is switched on by an inline script in viewer.html before first paint.
    document.getElementById('btn-page-only')?.addEventListener('click', enterPresent);
  })();

  // ── postMessage bridge to iframe (preserved protocol) ──────────────
  const frame = document.getElementById('dash-frame');
  const loadOverlay = document.getElementById('load-overlay');
  const loadSize = document.getElementById('load-size');

  // ── Annotate module (owner/admin only) ──────────────────────────────
  const ANNOT = (function () {
    if (!CAN_MANAGE) return null;
    const state = { items: [], includeSource: true, sourceText: null, sourceSize: 0, shot: null };
    const $list = document.getElementById('annot-list');
    const $count = document.getElementById('annot-count');
    const $status = document.getElementById('annotate-status');
    const $srcChk = document.getElementById('annot-include-source');
    // ADR-18: for an RLS report the checkbox comes from the template unchecked and
    // disabled — such a viewer would get a 403 for the source, so there is no point asking.
    if ($srcChk) state.includeSource = $srcChk.checked;
    const $srcSize = document.getElementById('annot-source-size');
    const $shotWrap = document.getElementById('annot-shot-wrap');
    const root = document.getElementById('viewer-root');

    function send(msg) { try { frame.contentWindow.postMessage(msg, '*'); } catch (e) { /* ignore */ } }
    function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

    function setPicking(on) {
      send({ type: on ? 'op_annotate_on' : 'op_annotate_off' });
      if (on) loadSource(); // lazy: fetched once, when Annotate is actually opened
      if ($status) {
        $status.textContent = on ? 'Picking is ON — click any element on the dashboard' : 'Picking paused — open the Annotate tab to resume';
        $status.style.color = on ? 'var(--accent)' : 'var(--ink-dim)';
      }
    }
    function open() {
      root?.setAttribute('data-dock', 'open');
      document.querySelector('.dock-tab[data-tab="annotate"]')?.click();
    }

    function render() {
      if ($count) $count.textContent = state.items.length ? `${state.items.length} element${state.items.length === 1 ? '' : 's'}` : 'No elements yet';
      if (!$list) return;
      $list.innerHTML = state.items.map(a => `
        <div class="annot-card" data-id="${a.id}">
          <div class="annot-card-head">
            <span class="annot-num">${a.id}</span>
            <code class="annot-sel" title="${esc(a.selector)}">${esc(a.selector)}</code>
            <button class="btn icon xs ghost annot-del" data-id="${a.id}" title="Remove"><svg class="ico ico-12"><use href="#ic-x"/></svg></button>
          </div>
          <textarea class="input sm annot-note" data-id="${a.id}" rows="2" placeholder="What should change here?">${esc(a.note)}</textarea>
        </div>`).join('');
      $list.querySelectorAll('.annot-note').forEach(tv => tv.addEventListener('input', () => {
        const it = state.items.find(x => x.id === +tv.dataset.id); if (it) it.note = tv.value;
      }));
      $list.querySelectorAll('.annot-del').forEach(b => b.addEventListener('click', () => removeItem(+b.dataset.id)));
    }

    function addPick(a) {
      // a.id crosses postMessage from the sandboxed iframe and render() puts it
      // straight into innerHTML; the controller contract says it is a counter,
      // so hold it to that — a string could carry markup, and === against
      // +dataset.id below would miss it anyway.
      a.id = Number(a.id);
      if (!Number.isFinite(a.id)) return;
      a.note = '';
      state.items.push(a);
      render();
    }
    function removeItem(id) { state.items = state.items.filter(x => x.id !== id); send({ type: 'op_annotate_remove', id }); render(); }
    function clearAll() { state.items = []; state.shot = null; send({ type: 'op_annotate_clear' }); render(); renderShot(); }

    // ── Annotated screenshot via tab capture (works cross-origin; html2canvas
    //    cannot, because the dashboard iframe is sandboxed to an opaque origin).
    function renderShot() {
      if (!$shotWrap) return;
      $shotWrap.innerHTML = state.shot
        ? `<img class="annot-thumb" src="${state.shot}" alt="annotated screenshot"/>
           <div class="annot-shot-actions">
             <button type="button" class="btn xs ghost" id="annot-copyimg">Copy image</button>
             <button type="button" class="btn xs ghost" id="annot-dlimg">Download</button>
             <button type="button" class="btn xs ghost" id="annot-rmimg">Remove</button>
           </div>`
        : '';
      document.getElementById('annot-copyimg')?.addEventListener('click', copyImg);
      document.getElementById('annot-dlimg')?.addEventListener('click', dlImg);
      document.getElementById('annot-rmimg')?.addEventListener('click', () => { state.shot = null; renderShot(); });
    }
    async function copyImg() {
      if (!state.shot) return;
      try {
        const blob = await (await fetch(state.shot)).blob();
        await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
        window.OP_TOAST('Image copied — paste it into the AI chat', 'success');
      } catch (e) { window.OP_TOAST('Copy not supported here — use Download', 'warning'); }
    }
    function dlImg() {
      if (!state.shot) return;
      const a = document.createElement('a'); a.href = state.shot; a.download = 'dashboard-annotated.png';
      document.body.appendChild(a); a.click(); a.remove();
    }
    async function captureScreenshot() {
      if (!navigator.mediaDevices?.getDisplayMedia) { window.OP_TOAST('Screen capture is not supported in this browser', 'error'); return; }
      if (!state.items.length) { window.OP_TOAST('Mark at least one element first', 'warning'); return; }
      let stream;
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 4 }, audio: false, preferCurrentTab: true });
      } catch (e) { window.OP_TOAST('Screen capture cancelled', 'warning'); return; }
      try {
        const video = document.createElement('video');
        video.muted = true; video.srcObject = stream;
        await video.play().catch(() => {});
        if (video.readyState < 2) await new Promise(r => video.addEventListener('loadeddata', r, { once: true }));
        await new Promise(r => setTimeout(r, 150)); // let a frame paint
        const vw = video.videoWidth, vh = video.videoHeight;
        const sx = vw / window.innerWidth, sy = vh / window.innerHeight;
        const r = frame.getBoundingClientRect();
        const l = Math.max(0, r.left), tv = Math.max(0, r.top);
        const cw = Math.max(1, Math.min(window.innerWidth, r.right) - l);
        const ch = Math.max(1, Math.min(window.innerHeight, r.bottom) - tv);
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(cw * sx); canvas.height = Math.round(ch * sy);
        canvas.getContext('2d').drawImage(video, Math.round(l * sx), Math.round(tv * sy), canvas.width, canvas.height, 0, 0, canvas.width, canvas.height);
        state.shot = canvas.toDataURL('image/png');
        renderShot();
        window.OP_TOAST('Screenshot captured', 'success');
      } catch (e) {
        window.OP_TOAST('Could not capture the screenshot', 'error');
      } finally {
        stream.getTracks().forEach(tr => tr.stop());
      }
    }

    async function loadSource() {
      if (state.sourceText != null) return state.sourceText;
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/source`);
        if (!res.ok) throw new Error();
        state.sourceText = await res.text();
        state.sourceSize = state.sourceText.length;
      } catch { state.sourceText = ''; state.sourceSize = 0; }
      updateSizeLabel();
      return state.sourceText;
    }
    function updateSizeLabel() {
      if (!$srcSize) return;
      if (!state.includeSource || !state.sourceSize) { $srcSize.textContent = ''; return; }
      const kb = (state.sourceSize / 1024).toFixed(0);
      const tok = Math.round(state.sourceSize / 4 / 100) * 100;
      $srcSize.textContent = `~${kb} KB · ~${tok.toLocaleString()} tok`;
    }

    function buildPrompt() {
      const L = [];
      L.push('You are editing a standalone HTML dashboard (a single self-contained .html file).');
      L.push('Apply the requested change(s) below and return the COMPLETE updated HTML file.');
      L.push('');
      L.push('# Requested changes');
      state.items.forEach(a => L.push(`${a.id}. [\`${a.selector}\`] ${a.note || '(no description — infer a sensible improvement)'}`));
      L.push('');
      L.push('# Annotated elements');
      state.items.forEach(a => {
        L.push(`## Element ${a.id}`);
        L.push(`- Selector: \`${a.selector}\``);
        L.push(`- Tag: \`${a.tag}\`${a.classes ? ' · classes: `' + a.classes + '`' : ''}${a.elId ? ' · id: `' + a.elId + '`' : ''}`);
        if (a.text) L.push(`- Text: ${a.text}`);
        const st = Object.entries(a.styles || {}).map(([k, v]) => `${k}: ${v}`).join('; ');
        if (st) L.push(`- Key styles: ${st}`);
        L.push('- HTML:');
        L.push('```html');
        L.push(a.html || '');
        L.push('```');
        L.push('');
      });
      if (state.shot) {
        L.push('> An annotated screenshot is attached to this message. The numbered markers (1, 2, …) correspond to the elements above.');
        L.push('');
      }
      if (state.includeSource && state.sourceText) {
        L.push('# Full dashboard source');
        L.push('```html');
        L.push(state.sourceText);
        L.push('```');
      }
      return L.join('\n');
    }

    async function copyPrompt() {
      if (!state.items.length) { window.OP_TOAST('Annotate at least one element first', 'warning'); return; }
      if (state.includeSource) await loadSource();
      try { await copyText(buildPrompt()); window.OP_TOAST('Prompt copied — paste into Claude / Codex / Qwen', 'success'); }
      catch { window.OP_TOAST('Could not copy', 'error'); }
    }

    document.getElementById('btn-annotate')?.addEventListener('click', open);
    document.getElementById('btn-annot-copy')?.addEventListener('click', copyPrompt);
    document.getElementById('btn-annot-clear')?.addEventListener('click', clearAll);
    document.getElementById('btn-annot-shot')?.addEventListener('click', captureScreenshot);
    $srcChk?.addEventListener('change', () => { state.includeSource = $srcChk.checked; if (state.includeSource) loadSource(); updateSizeLabel(); });
    // No eager loadSource(): the raw source (up to tens of MB) is fetched only
    // when Annotate is actually opened (setPicking) or the prompt needs it —
    // an eager fetch used to download the full document on EVERY viewer open
    // just to render the "~KB · ~tok" size label.

    return { addPick, setPicking };
  })();

  // ── Colors module (owner/admin only) ────────────────────────────────
  const THEME = (function () {
    if (!CAN_MANAGE) return null;

    // Corporate, Dark, Light preset palettes. Applied by mapping each of
    // the report's colours to the preset colour with the nearest luminance —
    // this re-skins while preserving the original light/dark structure.
    const PRESETS = {
      corporate: ['#0a2c5e', '#10396f', '#1565a8', '#2ba9e0', '#7fc4e8', '#bfe0f2', '#2e7d3a', '#3fa648', '#f4c20d', '#ffffff'],
      dark: ['#0d1117', '#161b22', '#21262d', '#30363d', '#58a6ff', '#8b949e', '#c9d1d9', '#f0f6fc', '#3fb950', '#f85149'],
      light: ['#ffffff', '#f6f8fa', '#eaeef2', '#d0d7de', '#0969da', '#57606a', '#1f2328', '#0a3069', '#1a7f37', '#cf222e'],
    };

    const state = { colors: [], vars: [], saved: { vars: {}, remap: {} }, preset: null, scanned: false };
    const over = { vars: {}, remap: {} };

    const $status = document.getElementById('theme-status');
    const $vars = document.getElementById('theme-vars');
    const $varsSection = document.getElementById('theme-vars-section');
    const $palette = document.getElementById('theme-palette');

    function send(msg) { try { frame.contentWindow.postMessage(msg, '*'); } catch (e) { /* ignore */ } }
    function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

    function toHex(c) {
      c = String(c || '').trim();
      if (/^#[0-9a-f]{6}$/i.test(c)) return c.toLowerCase();
      if (/^#[0-9a-f]{3}$/i.test(c)) return ('#' + c[1] + c[1] + c[2] + c[2] + c[3] + c[3]).toLowerCase();
      const m = c.match(/rgba?\(([^)]+)\)/i);
      if (m) {
        const p = m[1].split(',').map(s => parseFloat(s));
        return '#' + p.slice(0, 3).map(n => Math.max(0, Math.min(255, Math.round(n || 0))).toString(16).padStart(2, '0')).join('');
      }
      return '#000000';
    }
    function lum(c) {
      const h = toHex(c).slice(1);
      const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    }
    function nearest(pal, color) {
      const L = lum(color); let best = pal[0], bd = Infinity;
      pal.forEach(p => { const d = Math.abs(lum(p) - L); if (d < bd) { bd = d; best = p; } });
      return best;
    }

    let applyTimer;
    function pushApply() {
      clearTimeout(applyTimer);
      applyTimer = setTimeout(() => send({ type: 'op_theme_apply', theme: { vars: over.vars, remap: over.remap } }), 120);
    }

    function rowHtml(kind, key, orig, override, count) {
      const cur = override || orig;
      return `<div class="theme-row ${override ? 'changed' : ''}" data-kind="${kind}" data-key="${esc(key)}">
        <span class="theme-chip" style="background:${esc(orig)}"></span>
        <input type="color" class="theme-pick" value="${toHex(cur)}">
        <span class="theme-name" title="${esc(key)}">${esc(key)}</span>
        ${count ? `<span class="theme-count">×${Number(count) || ''}</span>` : ''}
        <button type="button" class="theme-reset" title="Reset to original"><svg class="ico ico-12"><use href="#ic-x"/></svg></button>
      </div>`;
    }

    function render() {
      if ($status) {
        $status.textContent = state.scanned
          ? `${state.colors.length} colour${state.colors.length === 1 ? '' : 's'}${state.vars.length ? ` · ${state.vars.length} variable${state.vars.length === 1 ? '' : 's'}` : ''} found`
          : 'Scanning colours…';
      }
      if ($varsSection) $varsSection.hidden = !state.vars.length;
      if ($vars) $vars.innerHTML = state.vars.map(v => rowHtml('var', v.name, v.value, over.vars[v.name], 0)).join('');
      if ($palette) {
        $palette.innerHTML = state.colors.length
          ? state.colors.map(c => rowHtml('color', c.value, c.value, over.remap[c.value], c.count)).join('')
          : (state.scanned ? '<div class="dock-hint">No recolourable colours found — the report may use canvas charts or a CDN stylesheet.</div>' : '');
      }
      bindRows();
    }

    function origOf(kind, key) {
      if (kind === 'var') { const v = state.vars.find(x => x.name === key); return v ? v.value : key; }
      return key;
    }
    function bindRows() {
      [$vars, $palette].forEach(cont => {
        cont && cont.querySelectorAll('.theme-row').forEach(row => {
          const kind = row.dataset.kind, key = row.dataset.key;
          row.querySelector('.theme-pick').addEventListener('input', e => setOverride(kind, key, e.target.value));
          row.querySelector('.theme-reset').addEventListener('click', () => clearOverride(kind, key));
        });
      });
    }
    function markRow(kind, key) {
      const store = kind === 'var' ? over.vars : over.remap;
      const cont = kind === 'var' ? $vars : $palette;
      const row = cont && [...cont.querySelectorAll('.theme-row')].find(r => r.dataset.key === key);
      if (row) row.classList.toggle('changed', key in store);
    }
    function setOverride(kind, key, val) {
      const store = kind === 'var' ? over.vars : over.remap;
      if (toHex(origOf(kind, key)) === toHex(val)) delete store[key]; else store[key] = val;
      state.preset = null;
      pushApply(); markRow(kind, key);
    }
    function clearOverride(kind, key) {
      const store = kind === 'var' ? over.vars : over.remap;
      delete store[key];
      state.preset = null;
      pushApply(); render();
    }

    function applyPreset(name) {
      const pal = PRESETS[name]; if (!pal) return;
      over.remap = {}; over.vars = {};
      state.colors.forEach(c => { over.remap[c.value] = nearest(pal, c.value); });
      state.vars.forEach(v => { over.vars[v.name] = nearest(pal, v.value); });
      state.preset = name;
      pushApply(); render();
    }
    function clearAll() { over.vars = {}; over.remap = {}; state.preset = null; pushApply(); render(); }
    function revert() {
      over.vars = { ...state.saved.vars }; over.remap = { ...state.saved.remap }; state.preset = null;
      pushApply(); render();
    }

    async function save() {
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/theme`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ vars: over.vars, remap: over.remap, preset: state.preset }),
        });
        if (!res.ok) throw new Error();
        state.saved = { vars: { ...over.vars }, remap: { ...over.remap } };
        window.OP_TOAST('Theme saved — everyone sees it now', 'success');
      } catch { window.OP_TOAST('Could not save theme', 'error'); }
    }
    function download(e) {
      const fname = (e?.currentTarget?.dataset?.filename) || 'theme.json';
      const data = JSON.stringify({ vars: over.vars, remap: over.remap, preset: state.preset }, null, 2);
      const url = URL.createObjectURL(new Blob([data], { type: 'application/json' }));
      const a = document.createElement('a');
      a.href = url; a.download = fname; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      window.OP_TOAST('Theme downloaded', 'success');
    }

    let scanTries = 0, scanTimer;
    function requestScan() {
      send({ type: 'op_theme_scan' });
      scanTries++;
      if (scanTries < 6) scanTimer = setTimeout(() => { if (!state.scanned) requestScan(); }, 500);
    }
    function onTab(tab) {
      if (tab === 'colors') { if (!state.scanned) requestScan(); }
      else stopPick();
    }

    // ── Colour inspector: click an element in the report → highlight its rows ──
    let picking = false;
    function setPick(on) {
      picking = on;
      send({ type: on ? 'op_theme_pick_on' : 'op_theme_pick_off' });
      const b = document.getElementById('btn-theme-pick');
      if (b) {
        b.classList.toggle('active', on);
        const lbl = b.querySelector('span');
        if (lbl) lbl.textContent = on ? 'Click elements… (click here to stop)' : 'Pick colour from report';
      }
    }
    function stopPick() { if (picking) setPick(false); }
    function onPicked(msg) {
      const cols = msg.colors || [];
      [$vars, $palette].forEach(c => c && c.querySelectorAll('.theme-row.hit').forEach(r => r.classList.remove('hit')));
      let first = null, matched = 0;
      [$vars, $palette].forEach(cont => {
        cont && cont.querySelectorAll('.theme-row').forEach(row => {
          const kind = row.dataset.kind, key = row.dataset.key;
          const val = kind === 'var' ? (state.vars.find(v => v.name === key) || {}).value : key;
          if (val && cols.some(c => toHex(c) === toHex(val))) { row.classList.add('hit'); matched++; if (!first) first = row; }
        });
      });
      if (first) first.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      if ($status) $status.textContent = matched
        ? `Picked ${cols.length} colour${cols.length === 1 ? '' : 's'} → ${matched} row${matched === 1 ? '' : 's'} highlighted`
        : 'That colour isn’t in the editable palette (canvas/CDN?).';
    }

    function onPalette(msg) {
      state.colors = msg.colors || [];
      state.vars = msg.vars || [];
      state.scanned = true;
      clearTimeout(scanTimer);
      render();
      pushApply(); // keep preview in sync with the editor's overrides
    }

    async function loadSaved() {
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/theme`);
        if (!res.ok) return;
        const tv = await res.json();
        over.vars = { ...(tv.vars || {}) };
        over.remap = { ...(tv.remap || {}) };
        state.saved = { vars: { ...over.vars }, remap: { ...over.remap } };
        state.preset = tv.preset || null;
      } catch { /* no saved theme */ }
    }

    document.getElementById('btn-theme-save')?.addEventListener('click', save);
    document.getElementById('btn-theme-download')?.addEventListener('click', download);
    document.getElementById('btn-theme-revert')?.addEventListener('click', revert);
    document.getElementById('theme-presets')?.addEventListener('click', e => {
      const b = e.target.closest('[data-preset]'); if (!b) return;
      if (b.dataset.preset === 'clear') clearAll(); else applyPreset(b.dataset.preset);
    });
    document.getElementById('btn-theme-pick')?.addEventListener('click', () => setPick(!picking));
    loadSaved();

    return { onTab, onPalette, onPicked, stopPick };
  })();

  // ── Text module (owner/admin only): edit static text directly on the report ──
  const TEXT = (function () {
    if (!CAN_MANAGE) return null;
    const state = { edits: {}, originals: {}, saved: {}, editing: false };
    const $list = document.getElementById('text-list');
    const $count = document.getElementById('text-count');
    const $status = document.getElementById('text-status');

    function send(msg) { try { frame.contentWindow.postMessage(msg, '*'); } catch (e) { /* ignore */ } }
    function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
    function trim(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n) + '…' : s; }

    function setEdit(on) {
      state.editing = on;
      send({ type: on ? 'op_text_edit_on' : 'op_text_edit_off' });
      const b = document.getElementById('btn-text-edit');
      if (b) { b.classList.toggle('active', on); const lbl = b.querySelector('span'); if (lbl) lbl.textContent = on ? 'Editing… (click here to stop)' : 'Edit text'; }
      if ($status) $status.textContent = on ? 'Click any text on the report and type (Enter to confirm)' : 'Turn on, then click any text';
    }
    function stopEdit() { if (state.editing) setEdit(false); }

    function render() {
      const keys = Object.keys(state.edits);
      if ($count) $count.textContent = keys.length ? `${keys.length} edit${keys.length === 1 ? '' : 's'}` : 'No edits yet';
      if (!$list) return;
      $list.innerHTML = keys.map(sel => `
        <div class="annot-card" data-sel="${esc(sel)}">
          <div class="annot-card-head">
            <code class="annot-sel" title="${esc(sel)}">${esc(sel)}</code>
            <button class="btn icon xs ghost text-del" data-sel="${esc(sel)}" title="Revert this"><svg class="ico ico-12"><use href="#ic-x"/></svg></button>
          </div>
          <div class="text-diff">${sel in state.originals ? `<span class="text-old">${esc(trim(state.originals[sel], 120))}</span> ` : ''}<span class="text-new">${esc(trim(state.edits[sel], 120))}</span></div>
        </div>`).join('');
      $list.querySelectorAll('.text-del').forEach(b => b.addEventListener('click', () => removeItem(b.dataset.sel)));
    }

    function onEdited(msg) {
      if (!msg.selector) return;
      if (msg.text === msg.original) { delete state.edits[msg.selector]; }
      else {
        state.edits[msg.selector] = msg.text;
        if (!(msg.selector in state.originals)) state.originals[msg.selector] = msg.original;
      }
      render();
    }
    function removeItem(sel) { delete state.edits[sel]; send({ type: 'op_text_apply', edits: state.edits }); render(); }
    function clearAll() { state.edits = {}; send({ type: 'op_text_apply', edits: {} }); render(); }
    function revert() { state.edits = { ...state.saved }; send({ type: 'op_text_apply', edits: state.edits }); render(); }

    async function save() {
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/text`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ edits: state.edits }),
        });
        if (!res.ok) throw new Error();
        state.saved = { ...state.edits };
        window.OP_TOAST('Text saved — everyone sees it now', 'success');
      } catch { window.OP_TOAST('Could not save text', 'error'); }
    }
    async function loadSaved() {
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/text`);
        if (res.ok) { const tv = await res.json(); state.edits = { ...(tv.edits || {}) }; state.saved = { ...state.edits }; }
      } catch { /* none */ }
      render();
    }

    function onTab(tab) { if (tab !== 'text') stopEdit(); }

    document.getElementById('btn-text-edit')?.addEventListener('click', () => setEdit(!state.editing));
    document.getElementById('btn-text-save')?.addEventListener('click', save);
    document.getElementById('btn-text-revert')?.addEventListener('click', revert);
    document.getElementById('btn-text-clear')?.addEventListener('click', clearAll);
    loadSaved();

    return { onTab, onEdited, stopEdit };
  })();

  // Open what the navigator page asked for. Decision and execution live in
  // page-links.js (shared with the share page); only the window binding is here.
  var PAGE_OPENER = PAGE_LINKS ? PAGE_LINKS.makeOpener({ t: t }) : null;
  function openFromPage(raw, target) { if (PAGE_OPENER) PAGE_OPENER.open(raw, target); }

  // 1. iframe announces ready, sends declared slots
  window.addEventListener('message', async (e) => {
    // Only the dashboard frame speaks this protocol. This does NOT make the
    // payload trustworthy — the injected controller and the report's own

    // scripts share that window, so a hostile dashboard passes this check —
    // which is why every field is still escaped or coerced at its sink. What
    // it does close is a second window (an opener, a popup, another tab)
    // driving the dock.
    if (frame && e.source !== frame.contentWindow) return;
    const msg = e.data;
    if (!msg || !msg.type) return;

    // The navigator asks to open a link. ALL decisions are made here, outside
    // the sandbox: the page author's code lives inside it, and its checks
    // cannot be trusted by construction. The tab itself is opened from here too — opened
    // from inside, it would inherit the sandbox and get no cookies, i.e. it would
    // show a blank screen on any sign-in (PBIRS, 29.10).
    if (msg.type === 'op_open') { openFromPage(msg.url, msg.target); return; }

    if (msg.type === 'op_pick') { if (ANNOT) ANNOT.addPick(msg.annotation); return; }
    if (msg.type === 'op_theme_palette') { if (THEME) THEME.onPalette(msg); return; }
    if (msg.type === 'op_theme_picked') { if (THEME) THEME.onPicked(msg); return; }
    if (msg.type === 'op_text_edited') { if (TEXT) TEXT.onEdited(msg); return; }

    if (msg.type === 'op_height' && frame) {
      frame.style.height = msg.height + 'px';
      return;
    }

    if (msg.type === 'op_save_ls' && CAN_UPDATE_DATA) {
      try {
        await fetch(`/api/dashboards/${DASHBOARD_ID}/localstorage`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(msg.data),
        });
      } catch (err) {
        console.warn('localStorage sync failed', err);
      }
      return;
    }

    // Snapshot dashboards that edit their own data in-page (e.g. the HR roadmap)
    // post their full state as op_save_snapshot. Persist it as the version's
    // snapshot.json via the existing endpoint so edits survive a reload — owner/
    // admin only (CAN_UPDATE_DATA); the server re-checks can_update_dashboard_data.
    // Share-link viewers use share.html, which has no such handler, so anonymous
    // visitors can never write.
    if (msg.type === 'op_save_snapshot' && CAN_UPDATE_DATA) {
      const status = document.getElementById('file-status');
      try {
        const fd = new FormData();
        fd.append('file', new Blob([JSON.stringify(msg.data)], { type: 'application/json' }), 'snapshot.json');
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/snapshot`, { method: 'PUT', body: fd });
        if (!res.ok) throw new Error('save failed');
        if (status) { status.textContent = 'Saved'; status.style.color = 'var(--success)'; setTimeout(() => { if (status) status.textContent = ''; }, 2000); }
      } catch (err) {
        console.warn('snapshot sync failed', err);
        if (status) { status.textContent = 'Save failed'; status.style.color = 'var(--danger)'; }
      }
      return;
    }

    if (msg.type === 'op_ready') {
      try { performance.mark('op:ready'); } catch (e2) {}
      // The chat frame is created only after this — a report load must cost
      // no /explore request (hot path, plan P1).
      try { CHAT && CHAT.onReportReady(); } catch (e2) {}
      // Send current slot files to iframe
      for (const slot of SLOTS || []) {
        try {
          const t0 = `op:slot${slot.slot}:t0`;
          performance.mark(t0);
          // ?v= makes the url content-addressed (storage.slot_fp): a publish
          // mints a new url, so a fresh report can never be answered from the
          // browser cache, and an unchanged one costs no request at all.
          const res = await fetch(`/render/${DASHBOARD_ID}/files/${slot.slot}`
            + (slot.v ? `?v=${slot.v}` : ''));
          if (!res.ok) continue;
          const buf = await res.arrayBuffer();
          performance.measure(`op:slot_load:${slot.slot}`, t0);
          frame.contentWindow.postMessage({
            type: 'op_file',
            slot: slot.slot,
            name: slot.name,
            data: buf
          }, '*', [buf]);
        } catch (err) {
          console.error('slot load failed', slot.slot, err);
        }
      }
      try { performance.measure('op:ready_to_data', 'op:ready'); } catch (e2) {}
      if (loadOverlay) loadOverlay.classList.remove('active');
    }

    // 2. iframe → parent on user file change (PUT to server)
    if (msg.type === 'op_save_file' && CAN_UPDATE_DATA) {
      const { slot, name, data } = msg;
      const fd = new FormData();
      fd.append('file', new Blob([data]), name);
      const status = document.getElementById('file-status');
      if (status) { status.textContent = `Saving slot ${slot}…`; status.style.color = 'var(--accent)'; }
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/files/${slot}`, { method: 'PUT', body: fd });
        if (!res.ok) throw new Error('save failed');
        if (status) { status.textContent = `Saved slot ${slot}`; status.style.color = 'var(--success)'; }
        setTimeout(() => { if (status) status.textContent = ''; }, 2500);
      } catch (err) {
        if (status) { status.textContent = 'Save failed'; status.style.color = 'var(--danger)'; }
      }
    }
  });

  // ── Drop-to-replace overlay (slot 0) ────────────────────────────────
  if (CAN_UPDATE_DATA) {
    const overlay = document.getElementById('drop-overlay');
    let depth = 0;
    window.addEventListener('dragenter', e => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
      depth++;
      overlay?.classList.add('visible');
    });
    window.addEventListener('dragleave', () => {
      depth--;
      if (depth <= 0) { depth = 0; overlay?.classList.remove('visible', 'active'); }
    });
    window.addEventListener('dragover', e => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
      e.preventDefault();
      overlay?.classList.add('active');
    });
    window.addEventListener('drop', async e => {
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      e.preventDefault();
      depth = 0;
      overlay?.classList.remove('visible', 'active');
      const file = e.dataTransfer.files[0];
      const fd = new FormData();
      fd.append('file', file, file.name);
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/files/0`, { method: 'PUT', body: fd });
        if (!res.ok) throw new Error();
        window.OP_TOAST(`Replaced slot 0 with ${file.name}`, 'success');
        setTimeout(() => location.reload(), 500);
      } catch (err) {
        window.OP_TOAST('Upload failed', 'error');
      }
    });
  }

  // ── Settings: title/description PATCH ───────────────────────────────
  document.getElementById('form-meta')?.addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = Object.fromEntries(fd);
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error();
      window.OP_TOAST('Saved', 'success');
    } catch {
      window.OP_TOAST('Save failed', 'error');
    }
  });

  // ── Visibility toggles ──────────────────────────────────────────────
  document.getElementById('toggle-published')?.addEventListener('op:toggle', async (e) => {
    await fetch(`/api/dashboards/${DASHBOARD_ID}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_published: e.detail.checked })
    });
  });
  document.getElementById('toggle-public')?.addEventListener('op:toggle', async (e) => {
    await fetch(`/api/dashboards/${DASHBOARD_ID}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_public: e.detail.checked })
    });
    IS_PUBLIC = e.detail.checked;
  });

  // ── "Strictly confidential": enable, change password, disable ───────
  // Reloading after every change is deliberate: the mode redraws half of the
  // dock (Share tab, single-file download, badge), and patching that by hand
  // on the client would mean keeping a second truth about the report's state.
  const confState = document.getElementById('conf-state');
  function confSay(text) { if (confState) confState.textContent = text; }

  document.getElementById('btn-conf-set')?.addEventListener('click', async () => {
    const field = document.getElementById('conf-new');
    const value = field ? field.value : '';
    if (!value || value.length < 8) {
      confSay(t('The password must be at least 8 characters.'));
      field?.focus();
      return;
    }
    const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/confidential`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: value })
    });
    if (field) field.value = '';
    if (!res.ok) { confSay(t('Did not work: error {p1}.').replace('{p1}', String(res.status))); return; }
    location.reload();
  });

  document.getElementById('btn-conf-off')?.addEventListener('click', async () => {
    if (!confirm(t('Turn the mode off? The report will open without a password again, and links and single-file export become available.'))) return;
    const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/confidential`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: null })
    });
    if (!res.ok) { confSay(t('Did not work: error {p1}.').replace('{p1}', String(res.status))); return; }
    location.reload();
  });

  // ── Permissions: list + grant + revoke ──────────────────────────────
  async function loadPermissions() {
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/permissions`);
      if (!res.ok) return;
      const data = await res.json();
      renderPermissions(data.permissions || data || []);
    } catch {}
  }
  function renderPermissions(list) {
    const wrap = document.getElementById('perm-list');
    const count = document.getElementById('perm-count');
    if (!wrap) return;
    if (count) count.textContent = `${list.length} user${list.length === 1 ? '' : 's'} with access`;
    wrap.innerHTML = list.map(p => `
      <div class="perm-row" data-uid="${p.user_id || p.id}">
        <div class="who-block">
          <span class="avatar mono" style="width:22px;height:22px;border-radius:4px;background:var(--surface-2);display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--ink-muted)">
            ${(p.person_id || '').slice(0,2).toUpperCase()}
          </span>
          <div>
            <div class="person_id">${p.person_id}</div>
            <div class="role">${p.can_update_data ? 'can update' : 'read-only'}</div>
          </div>
        </div>
        <button class="btn icon xs ghost revoke" data-revoke="${p.user_id || p.id}">
          <svg class="ico ico-12"><use href="#ic-x"/></svg>
        </button>
      </div>
    `).join('');
  }
  document.getElementById('form-perm')?.addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const person_id = fd.get('person_id');
    const can_update_data = fd.get('can_update_data') === 'on';
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/permissions/${person_id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ can_update_data })
      });
      if (!res.ok) throw new Error();
      window.OP_TOAST(`Granted access to ${person_id}`, 'success');
      e.target.reset();
      loadPermissions();
    } catch {
      window.OP_TOAST('Grant failed', 'error');
    }
  });
  document.getElementById('perm-list')?.addEventListener('click', async e => {
    const btn = e.target.closest('[data-revoke]');
    if (!btn) return;
    const uid = btn.dataset.revoke;
    if (!confirm('Revoke access?')) return;
    await fetch(`/api/dashboards/${DASHBOARD_ID}/permissions/${uid}`, { method: 'DELETE' });
    loadPermissions();
  });

  // ── Slot upload (interactive) ───────────────────────────────────────
  document.querySelectorAll('.slot-delete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const slot = btn.dataset.slot;
      if (!confirm(`Remove file from slot ${slot}? The dashboard will reload.`)) return;
      btn.disabled = true;
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/files/${slot}`, { method: 'DELETE' });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          window.OP_TOAST(err.detail || `Error ${res.status}`, 'error');
          btn.disabled = false;
          return;
        }
        window.OP_TOAST(`Slot ${slot} cleared`, 'success');
        setTimeout(() => location.reload(), 500);
      } catch {
        window.OP_TOAST('Network error', 'error');
        btn.disabled = false;
      }
    });
  });

  document.getElementById('btn-upload-slots')?.addEventListener('click', async () => {
    const inputs = document.querySelectorAll('.slot-file-input');
    const btn = document.getElementById('btn-upload-slots');
    const msg = document.getElementById('slots-msg');
    const selected = [...inputs].filter(i => i.files[0]);
    if (!selected.length) { window.OP_TOAST('Select at least one file', 'warning'); return; }
    if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }
    let uploaded = 0;
    for (const inp of selected) {
      const fd = new FormData();
      fd.append('file', inp.files[0], inp.files[0].name);
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/files/${inp.dataset.slot}`, { method: 'PUT', body: fd });
        if (res.ok) uploaded++;
        else {
          const err = await res.json().catch(() => ({}));
          window.OP_TOAST(`Slot ${inp.dataset.slot}: ${err.detail || res.status}`, 'error');
        }
      } catch { window.OP_TOAST(`Slot ${inp.dataset.slot}: network error`, 'error'); }
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Upload slots'; }
    if (msg) msg.textContent = `Uploaded ${uploaded} of ${selected.length} slot${selected.length === 1 ? '' : 's'}`;
    if (uploaded > 0) setTimeout(() => location.reload(), 700);
  });

  // ── Generator run history (Data panel, lazy-loaded on click) ────────
  (function () {
    const toggle = document.getElementById('gen-runs-toggle');
    const wrap = document.getElementById('gen-runs');
    if (!toggle || !wrap) return;

    function fmtDate(iso) {
      if (!iso) return '—';
      const d = new Date(iso);
      if (isNaN(d.getTime())) return '—';
      const pad = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    function fmtDur(ms) {
      return ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`;
    }
    function hintRow(text, danger) {
      const row = document.createElement('div');
      row.className = 'dock-hint';
      row.style.marginBottom = '4px';
      if (danger) row.style.color = 'var(--danger)';
      row.textContent = text;
      return row;
    }

    function renderRuns(runs) {
      wrap.textContent = '';
      if (!runs.length) { wrap.appendChild(hintRow('No runs yet')); return; }
      runs.forEach(r => {
        wrap.appendChild(hintRow(
          `${r.status} · ${r.engine} · gen v${r.version_number != null ? r.version_number : '?'} · ` +
          `${fmtDate(r.started_at)} · ${fmtDur(r.duration_ms)} · ${r.started_by || '—'}`
        ));
        if (r.status === 'error' && r.error_message) {
          const errRow = hintRow(r.error_message, true);
          errRow.style.whiteSpace = 'nowrap';
          errRow.style.overflow = 'hidden';
          errRow.style.textOverflow = 'ellipsis';
          errRow.title = r.error_message;
          wrap.appendChild(errRow);
        }
        if (Array.isArray(r.aux_files) && r.aux_files.length) {
          const row = document.createElement('div');
          row.className = 'dock-hint';
          row.style.marginBottom = '4px';
          r.aux_files.forEach((name, i) => {
            if (i) row.appendChild(document.createTextNode(', '));
            const a = document.createElement('a');
            a.href = `/api/dashboards/${wrap.dataset.dashboardId}/generator/runs/${r.id}/aux?name=${encodeURIComponent(name)}`;
            a.textContent = name;
            a.setAttribute('download', name);
            row.appendChild(a);
          });
          wrap.appendChild(row);
        }
      });
    }

    async function loadRuns() {
      wrap.textContent = '';
      wrap.appendChild(hintRow('Loading…'));
      try {
        const res = await fetch(`/api/dashboards/${wrap.dataset.dashboardId}/generator/runs?limit=20`);
        if (!res.ok) throw new Error();
        renderRuns(await res.json());
        wrap.dataset.loaded = '1';
      } catch {
        wrap.textContent = '';
        wrap.appendChild(hintRow('failed to load history', true));
      }
    }

    toggle.addEventListener('click', () => {
      if (wrap.dataset.loaded === '0') {
        wrap.hidden = false;
        loadRuns();
      } else {
        wrap.hidden = !wrap.hidden;
      }
    });
  })();

  // ── Snapshot upload (snapshot type) ─────────────────────────────────
  document.getElementById('form-snapshot')?.addEventListener('submit', async e => {
    e.preventDefault();
    const fileInput = e.target.querySelector('input[type="file"]');
    if (!fileInput?.files[0]) { window.OP_TOAST('Select a JSON file first', 'warning'); return; }
    const fd = new FormData(e.target);
    const btn = e.target.querySelector('button[type="submit"]');
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/snapshot`, { method: 'PUT', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Server error ${res.status}`);
      }
      window.OP_TOAST('Snapshot updated', 'success');
      setTimeout(() => location.reload(), 500);
    } catch (err) {
      window.OP_TOAST('Upload failed: ' + err.message, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
  });

  // ── Download current HTML version ───────────────────────────────────
  document.getElementById('btn-download-html')?.addEventListener('click', async e => {
    const btn = e.currentTarget;
    const fname = btn.dataset.filename || 'dashboard.html';
    btn.disabled = true;
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/source`);
      if (!res.ok) throw new Error();
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = fname;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      window.OP_TOAST('Downloaded current version', 'success');
    } catch {
      window.OP_TOAST('Download failed', 'error');
    } finally {
      btn.disabled = false;
    }
  });

  // ── Download as one file together with the data ─────────────────────
  // The build runs on the server (the slots live there), so all we do here is wait:
  // for a report of tens of megabytes that is seconds, and the button must show it —
  // otherwise the person clicks it a second time. The refusal reason is given in the
  // server's words (row-level access, size limit), not a generic "something went wrong".
  document.getElementById('btn-download-standalone')?.addEventListener('click', async e => {
    const btn = e.currentTarget;
    const fname = btn.dataset.filename || 'dashboard-data.html';
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = t('Building the file with data…');
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/standalone`);
      if (!res.ok) {
        let reason = '';
        try { reason = (await res.json()).detail || ''; } catch { /* not JSON */ }
        throw new Error(reason);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = fname;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      window.OP_TOAST(t('Done: the report and its data in one file'), 'success');
    } catch (err) {
      window.OP_TOAST(err && err.message ? err.message : t('Export failed'), 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  });

  // ── Restore a previous version ──────────────────────────────────────
  // The button in the versions panel was drawn but did nothing: there was no
  // handler, and the page never received the version list. A restore puts the
  // old content in as a NEW version — history is not rewritten — and the data
  // stays fresh meanwhile, so we ask once, explicitly.
  document.querySelectorAll('[data-restore]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const n = btn.dataset.restore;
      // The key is a whole phrase with a parameter: a translation must not be glued from pieces.
      if (!confirm(t('Restore version v{n}? It lands as a new version on top of the current one; the data stays today\'s.')
                     .replace('{n}', n))) return;
      btn.disabled = true;
      try {
        const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/versions/${n}/restore`,
                                { method: 'POST' });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          const msg = (body.detail && (body.detail.message || body.detail)) || 'Restore failed';
          window.OP_TOAST(typeof msg === 'string' ? msg : 'Restore failed', 'error');
          return;
        }
        const out = await res.json();
        window.OP_TOAST(t('Restored from v{from} — now v{to}')
                          .replace('{from}', n).replace('{to}', out.version), 'success');
        setTimeout(() => location.reload(), 700);
      } catch {
        window.OP_TOAST('Restore failed', 'error');
      } finally {
        btn.disabled = false;
      }
    });
  });

  // ── Replace HTML form ───────────────────────────────────────────────
  document.getElementById('form-replace-html')?.addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const post = () => fetch(`/api/dashboards/${DASHBOARD_ID}/version`, { method: 'POST', body: fd });
    try {
      let res = await post();
      if (res.status === 409) {
        // Server asks to confirm a dashboard-type change (usually a wrong file).
        let d = null;
        try { d = (await res.json()).detail; } catch { /* not json */ }
        if (!d || d.code !== 'type_change') throw new Error();
        if (!confirm(`Report type changes: ${d.from} → ${d.to}.\nThis usually means the wrong file was uploaded. Replace anyway?`)) return;
        fd.append('confirm_type_change', '1');
        res = await post();
      }
      if (!res.ok) {
        let msg = 'Upload failed';
        try {
          const d = (await res.json()).detail;
          if (typeof d === 'string') msg = d;
        } catch { /* keep generic */ }
        window.OP_TOAST(msg, 'error');
        return;
      }
      window.OP_TOAST('New version uploaded', 'success');
      setTimeout(() => location.reload(), 500);
    } catch {
      window.OP_TOAST('Upload failed', 'error');
    }
  });

  // ── Delete dashboard ────────────────────────────────────────────────
  document.getElementById('btn-delete')?.addEventListener('click', async () => {
    if (!confirm('Delete this dashboard and all its versions? This cannot be undone.')) return;
    const res = await fetch(`/api/dashboards/${DASHBOARD_ID}`, { method: 'DELETE' });
    if (res.ok) location.href = '/';
    else window.OP_TOAST('Delete failed', 'error');
  });

  // ── Open to all toggle button ───────────────────────────────────────
  document.getElementById('btn-publish-all')?.addEventListener('click', async () => {
    const next = !IS_PUBLIC;
    const res = await fetch(`/api/dashboards/${DASHBOARD_ID}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_public: next })
    });
    if (res.ok) {
      IS_PUBLIC = next;
      window.OP_TOAST(next ? 'Open to all users' : 'Access restricted', 'success');
      setTimeout(() => location.reload(), 400);
    }
  });

  // ── Share link generation ───────────────────────────────────────────
  async function loadShareLinks() {
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/share-links`);
      if (!res.ok) return;
      const list = await res.json();
      renderShareLinks(list.links || list || []);
    } catch {}
  }
  // The dock modules each keep their own esc(); this part of the file is outside
  // all three closures and had none, which is how the share label reached
  // innerHTML raw. The label is author-supplied and read back by any admin.
  function escShare(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
  function renderShareLinks(list) {
    const wrap = document.getElementById('share-list');
    if (!wrap) return;
    wrap.innerHTML = list.map(l => `
      <div class="share-link-row">
        <div style="flex:1">
          ${l.label ? `<div style="color:var(--ink);font-size:12px;font-family:var(--font-sans)">${escShare(l.label)}</div>` : ''}
          <div class="url">${escShare(l.url)}</div>
        </div>
        <span class="expires">${escShare(l.expires_in) || 'no expiry'}</span>
        <button class="btn icon xs ghost" data-copy-share="${escShare(l.url)}"><svg class="ico ico-12"><use href="#ic-link"/></svg></button>
        <button class="btn icon xs ghost" data-revoke-share="${escShare(l.id)}"><svg class="ico ico-12"><use href="#ic-x"/></svg></button>
      </div>
    `).join('');
  }
  document.getElementById('btn-gen-share')?.addEventListener('click', async () => {
    const expires = document.getElementById('share-expires').value;
    const label = document.getElementById('share-label').value;
    try {
      const res = await fetch(`/api/dashboards/${DASHBOARD_ID}/share-links`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expires_in_days: expires ? parseInt(expires) : null, label })
      });
      if (!res.ok) throw new Error();
      const link = await res.json();
      document.getElementById('share-url').value = location.origin + link.url;
      document.getElementById('share-result').style.display = '';
      loadShareLinks();
    } catch {
      window.OP_TOAST('Could not generate link', 'error');
    }
  });
  document.getElementById('btn-copy-share-url')?.addEventListener('click', () => {
    const v = document.getElementById('share-url').value;
    copyText(v);
    window.OP_TOAST('Copied', 'success');
  });
  document.getElementById('share-list')?.addEventListener('click', async e => {
    const copy = e.target.closest('[data-copy-share]');
    if (copy) {
      copyText(location.origin + copy.dataset.copyShare);
      window.OP_TOAST('Copied', 'success');
      return;
    }
    const revoke = e.target.closest('[data-revoke-share]');
    if (revoke) {
      if (!confirm('Revoke this share link?')) return;
      await fetch(`/api/dashboards/${DASHBOARD_ID}/share-links/${revoke.dataset.revokeShare}`, { method: 'DELETE' });
      loadShareLinks();
    }
  });

  // ── Initial load ────────────────────────────────────────────────────
  if (CAN_MANAGE) loadPermissions();
  // share follows the update right; no list on the page = links are off here (RLS)
  if ((CAN_MANAGE || CAN_UPDATE_DATA) && document.getElementById('share-list')) loadShareLinks();

  // ── Deep-link to a dock tab (e.g. /d/{id}?dock=colors from the Themes page).
  //    Runs after ANNOT/THEME are defined so the tab handler can reach them. ──
  (function deepLinkDock() {
    if (!CAN_MANAGE) return;
    const tab = new URLSearchParams(location.search).get('dock');
    if (!tab) return;
    document.getElementById('viewer-root')?.setAttribute('data-dock', 'open');
    document.querySelector(`.dock-tab[data-tab="${tab}"]`)?.click();
  })();
})();
