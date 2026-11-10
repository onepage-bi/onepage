/* OnePage — presentations.
 *
 * Two modes in one file, picked by what is on the page:
 *
 *   builder  (#pres-data)  — /presentations/{id}: slide list, drag-and-drop
 *                            order, notes, uploads, "Check before presenting".
 *   show     (#pres-bar)   — /d/{id}?pres=…&i=… and /presentations/{id}/slide/{k}:
 *                            keyboard navigation over the presenter bar's links.
 *
 * Every string that came from a person (deck title, report title, note, file
 * name) is written with textContent — never innerHTML. The only innerHTML in
 * this file carries markup this file wrote itself.
 *
 * The top block is pure: no DOM, no window — tests/presentations_dom.js
 * requires this file in node and calls it directly.
 */
(function () {
  'use strict';

  // ═════════════════════════════════════════════════════════════════
  // Pure helpers (exported for node tests)
  // ═════════════════════════════════════════════════════════════════

  const API = '/api/presentations';
  const PICKABLE_URL = API + '/pickable';

  const enc = encodeURIComponent;
  function presApiUrl(pid) { return API + '/' + enc(pid); }
  function itemsUrl(pid) { return presApiUrl(pid) + '/items'; }
  function itemUrl(pid, iid) { return presApiUrl(pid) + '/items/' + enc(iid); }
  function orderUrl(pid) { return presApiUrl(pid) + '/order'; }
  function uploadUrl(pid) { return presApiUrl(pid) + '/upload'; }
  function checkUrl(pid) { return presApiUrl(pid) + '/check'; }
  function builderUrl(pid) { return '/presentations/' + enc(pid); }

  // The first slide's own url — a dashboard slide is the report's OWN viewer
  // (routing, not a copy); only image/pdf slides have a page of their own.
  function slideUrl(pid, item, k) {
    if (item && item.kind === 'dashboard' && item.dashboard_id) {
      return '/d/' + enc(item.dashboard_id) + '?pres=' + enc(pid) + '&i=' + k;
    }
    return '/presentations/' + enc(pid) + '/slide/' + k;
  }
  function firstSlideUrl(pid, items) {
    return (items && items.length) ? '/presentations/' + enc(pid) + '/show?i=0' : null;
  }

  // Keyboard map for show mode. One table, so the viewer bar and the slide
  // page can never drift apart on what Space does.
  const KEYMAP = {
    'ArrowRight': 'next', 'PageDown': 'next', ' ': 'next', 'Spacebar': 'next',
    'ArrowLeft': 'prev', 'PageUp': 'prev',
    'Escape': 'exit', 'f': 'full', 'F': 'full', 'а': 'full', 'А': 'full',
  };
  function navFor(key) { return KEYMAP[key] || null; }

  // Same rule as static/js/hotkeys.js: a shortcut must never fire while the
  // presenter is typing a note.
  function isTyping(target) {
    if (!target) return false;
    const tag = (target.tagName || '').toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (target.isContentEditable) return true;
    const ce = target.getAttribute && target.getAttribute('contenteditable');
    return ce === '' || ce === 'true';
  }

  // OriginGuard refuses a state-changing request whose body has no
  // Content-Type, so every JSON call sets it explicitly. The upload is the one
  // exception ON PURPOSE: FormData makes the browser write its own multipart
  // boundary, and setting Content-Type by hand would corrupt it.
  const JSON_HEADERS = { 'Content-Type': 'application/json' };

  function jsonRequest(method, body) {
    const init = { method: method, headers: JSON_HEADERS };
    if (body !== undefined) init.body = JSON.stringify(body);
    return init;
  }
  function uploadRequest(file, note) {
    const fd = new FormData();
    fd.append('file', file);
    if (note) fd.append('note', note);
    return { method: 'POST', body: fd };  // no headers — see above
  }

  function saveOrder(pid, ids) {
    return fetch(orderUrl(pid), jsonRequest('PUT', { ids: ids }));
  }
  function saveNote(pid, iid, note) {
    return fetch(itemUrl(pid, iid), jsonRequest('PATCH', { note: note }));
  }
  function saveTitle(pid, title) {
    return fetch(presApiUrl(pid), jsonRequest('PATCH', { title: title }));
  }
  function addDashboard(pid, dashboardId) {
    return fetch(itemsUrl(pid), jsonRequest('POST', {
      kind: 'dashboard', dashboard_id: dashboardId,
    }));
  }
  function uploadSlide(pid, file) {
    return fetch(uploadUrl(pid), uploadRequest(file));
  }
  function deleteItem(pid, iid) {
    return fetch(itemUrl(pid, iid), jsonRequest('DELETE'));
  }
  function createPresentation(title) {
    return fetch(API, jsonRequest('POST', { title: title }));
  }

  // Order payload = the DOM order, read off the list at drop time. The list is
  // the truth: whatever the user sees top-to-bottom is what the server stores.
  function orderIdsFrom(container, selector) {
    if (!container) return [];
    return Array.prototype.map.call(
      container.querySelectorAll(selector || '[data-id]'),
      function (el) { return el.dataset.id; }
    ).filter(Boolean);
  }

  const KIND_LABEL = { dashboard: t('report'), image: t('image'), pdf: 'pdf', html: 'html' };
  function kindLabel(kind) { return KIND_LABEL[kind] || kind; }

  // ── PDF → per-page slides ─────────────────────────────────────────
  // The split runs entirely in the browser (pdf-lib, vendored at
  // static/vendor/pdf-lib.min.js — committed, not gitignored, so it ships
  // in patches). Each page becomes its own uploaded PDF slide, so pages can
  // be reordered and deleted like any other slide. Pure: PDFLib comes in as
  // an argument, so the node test can require pdf-lib itself.
  function pdfPageName(name, k, n) {
    return t('{p1} — page {p2} of {p3}.pdf', {p1: String(name).replace(/\.pdf$/i, ''), p2: k, p3: n});
  }
  async function splitPdf(PDFLib, bytes) {
    const src = await PDFLib.PDFDocument.load(bytes, { ignoreEncryption: true });
    const n = src.getPageCount();
    const out = [];
    for (let i = 0; i < n; i++) {
      const doc = await PDFLib.PDFDocument.create();
      const pages = await doc.copyPages(src, [i]);
      doc.addPage(pages[0]);
      out.push(await doc.save());
    }
    return out;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      API: API, PICKABLE_URL: PICKABLE_URL, KEYMAP: KEYMAP, JSON_HEADERS: JSON_HEADERS,
      presApiUrl: presApiUrl, itemsUrl: itemsUrl, itemUrl: itemUrl, orderUrl: orderUrl,
      uploadUrl: uploadUrl, checkUrl: checkUrl, builderUrl: builderUrl,
      slideUrl: slideUrl, firstSlideUrl: firstSlideUrl,
      navFor: navFor, isTyping: isTyping, kindLabel: kindLabel,
      jsonRequest: jsonRequest, uploadRequest: uploadRequest,
      orderIdsFrom: orderIdsFrom,
      pdfPageName: pdfPageName, splitPdf: splitPdf,
      saveOrder: saveOrder, saveNote: saveNote, saveTitle: saveTitle,
      addDashboard: addDashboard, uploadSlide: uploadSlide, deleteItem: deleteItem,
      createPresentation: createPresentation,
    };
  }
  if (typeof document === 'undefined') return;  // node require() path stops here

  const toast = function (msg, kind) {
    if (typeof window !== 'undefined' && window.OP_TOAST) window.OP_TOAST(msg, kind);
  };

  // ═════════════════════════════════════════════════════════════════
  // Show mode — keyboard over the presenter bar's own links
  // ═════════════════════════════════════════════════════════════════

  (function showMode() {
    const bar = document.getElementById('pres-bar');
    if (!bar) return;
    const targets = {
      next: document.getElementById('pres-next'),
      prev: document.getElementById('pres-prev'),
      exit: document.getElementById('pres-exit'),
    };
    // Fullscreen needs a user gesture: the bar's button or the F key.
    // Inside the show shell the SHELL goes fullscreen (it outlives slide
    // changes); a slide opened on its own does it itself.
    const inShell = (function () { try { return window.parent !== window && window.parent.location.origin === location.origin; } catch (e) { return false; } })();
    function goFull() {
      if (inShell) { try { window.parent.postMessage({ type: 'op_pres_full' }, location.origin); } catch (e) {} return; }
      const el = document.documentElement;
      try {
        if (document.fullscreenElement) document.exitFullscreen();
        else if (el.requestFullscreen) el.requestFullscreen();
      } catch (e) { /* not allowed here */ }
    }
    // Tell the shell which slide this is and where next/prev live — the
    // shell preloads the neighbour into a hidden frame and swaps with a
    // crossfade, so "Next" never shows a black (or white) gap.
    if (inShell) {
      try {
        const m = /[?&]i=(\d+)/.exec(location.search) || /\/slide\/(\d+)/.exec(location.pathname);
        window.parent.postMessage({ type: 'op_pres_slide',
          i: m ? Number(m[1]) : null,
          next: targets.next && !targets.next.classList.contains('is-off') ? targets.next.getAttribute('href') : null,
          prev: targets.prev && !targets.prev.classList.contains('is-off') ? targets.prev.getAttribute('href') : null,
        }, location.origin);
      } catch (e) {}
      // Ready = the content painted: pdf after its first draw (the slide
      // page posts it itself), an image after load, anything else now.
      const img = document.getElementById('pres-image');
      const pdfCanvas = document.getElementById('pres-pdf-canvas');
      const htmlFrame = document.getElementById('pres-html-frame');
      const ready = () => { try { window.parent.postMessage({ type: 'op_pres_ready' }, location.origin); } catch (e) {} };
      if (img) { if (img.complete) ready(); else { img.addEventListener('load', ready, { once: true }); img.addEventListener('error', ready, { once: true }); } }
      else if (htmlFrame) { htmlFrame.addEventListener('load', ready, { once: true }); setTimeout(ready, 4000); }
      else if (!pdfCanvas) ready();   // the pdf page reports after its first render
    }
    const fullBtn = document.getElementById('pres-full');
    fullBtn && fullBtn.addEventListener('click', goFull);
    document.addEventListener('keydown', function (e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTyping(e.target)) return;
      const what = navFor(e.key);
      if (!what) return;
      if (what === 'full') { e.preventDefault(); goFull(); return; }
      const a = targets[what];
      // An edge link is present but inert (.is-off): there is simply no next
      // slide, and swallowing the key is the right answer.
      if (!a) return;
      e.preventDefault();
      if (a.classList.contains('is-off')) return;
      if (what === 'exit' && inShell) { try { window.top.location.href = a.getAttribute('href'); return; } catch (e) {} }
      if (inShell && (what === 'next' || what === 'prev')) {
        try { window.parent.postMessage({ type: 'op_pres_nav', url: a.getAttribute('href') }, location.origin); return; } catch (e) {}
      }
      window.location.href = a.getAttribute('href');
    });
    // Clicks on the bar's own links go through the shell too.
    if (inShell) {
      ['next', 'prev'].forEach(function (k) {
        const a = targets[k];
        a && a.addEventListener('click', function (e) {
          if (a.classList.contains('is-off')) { e.preventDefault(); return; }
          e.preventDefault();
          try { window.parent.postMessage({ type: 'op_pres_nav', url: a.getAttribute('href') }, location.origin); }
          catch (err) { window.location.href = a.getAttribute('href'); }
        });
      });
    }
    // The bar's own "exit" link leaves the shell too.
    const exitA = document.getElementById('pres-exit');
    if (exitA && inShell) exitA.setAttribute('target', '_top');
  })();

  // ═════════════════════════════════════════════════════════════════
  // Builder
  // ═════════════════════════════════════════════════════════════════

  (function builder() {
    const dataEl = document.getElementById('pres-data');
    if (!dataEl) return;

    let DATA = { id: '', title: '', items: [] };
    try { DATA = JSON.parse(dataEl.textContent || '{}'); } catch (e) { /* keep empty */ }
    const PID = DATA.id;
    let CHECK = {};  // position -> {pub_at, data_at, stale, access}

    const $list = document.getElementById('pres-list');
    const $empty = document.getElementById('pres-empty');
    const $count = document.getElementById('pres-count');
    const $show = document.getElementById('pres-show');
    const $titleInput = document.getElementById('pres-title-input');
    const $checkBtn = document.getElementById('pres-check-btn');
    const $checkOut = document.getElementById('pres-check-out');
    const $file = document.getElementById('pres-file');
    const $picker = document.getElementById('pres-picker');
    const $pickerList = document.getElementById('pres-picker-list');
    const $pickerSearch = document.getElementById('pres-picker-search');

    function el(tag, cls, text) {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    }

    // ── Slide list ────────────────────────────────────────────────
    function metaLine(item, idx) {
      const row = el('div', 'pres-meta');
      const c = CHECK[item.position != null ? item.position : idx];
      if (item.kind === 'dashboard') {
        if (c && c.access === false) {
          row.appendChild(el('span', 'pres-chip warn', t('no access')));
          row.appendChild(el('span', null, t('this report no longer opens')));
          return row;
        }
        if (c && c.pub_at) row.appendChild(el('span', null, t('publication {p1}', {p1: c.pub_at})));
        if (c && c.data_at) row.appendChild(el('span', null, t('· data as of {p1}', {p1: c.data_at})));
        if (c && c.stale) row.appendChild(el('span', 'pres-chip warn', t('outdated')));
        if (!c) row.appendChild(el('span', 'mono dim', '…'));
      } else {
        const kb = item.size_bytes ? t('{p1} KB', {p1: Math.round(item.size_bytes / 1024)}) : '';
        row.appendChild(el('span', null, [item.original_name || '', kb].filter(Boolean).join(' · ')));
      }
      return row;
    }

    function slideNode(item, idx) {
      const li = el('div', 'pres-slide');
      li.dataset.id = item.id;
      li.draggable = true;

      li.appendChild(el('span', 'pres-handle', '⠿'));

      // The thumbnail is the slide's face: the image itself, the report's
      // scaled live render, or a document glyph for a PDF page.
      const thumb = el('div', 'pres-thumb');
      if (item.kind === 'image' && item.file_url) {
        const im = document.createElement('img');
        im.src = item.file_url; im.alt = ''; im.loading = 'lazy';
        thumb.appendChild(im);
      } else if (item.kind === 'dashboard' && item.dashboard_id) {
        const fr = document.createElement('iframe');
        fr.className = 'pres-thumb-frame'; fr.tabIndex = -1; fr.setAttribute('aria-hidden', 'true');
        fr.setAttribute('scrolling', 'no'); fr.loading = 'lazy';
        fr.src = '/render/' + enc(item.dashboard_id) + '/';
        thumb.appendChild(fr);
      } else if (item.kind === 'html' && item.file_url) {
        const fr = document.createElement('iframe');
        fr.className = 'pres-thumb-frame'; fr.tabIndex = -1; fr.setAttribute('aria-hidden', 'true');
        fr.setAttribute('scrolling', 'no'); fr.loading = 'lazy';
        fr.setAttribute('sandbox', 'allow-scripts');   // user-authored html — dashboard-iframe model
        fr.src = item.file_url;
        // A Claude deck hides its thumbnail rail in "presenting" mode — a
        // thumb wants the slide, not the chrome (same message as the show page).
        fr.addEventListener('load', function () {
          const say = () => { try { fr.contentWindow.postMessage({ __omelette_presenting: true }, '*'); } catch (e) {} };
          say(); setTimeout(say, 600);
        });
        thumb.appendChild(fr);
      } else if (item.kind === 'pdf' && item.file_url) {
        // Drawn lazily by pdf.js (renderPdfThumbs) — the glyph stays until then.
        thumb.classList.add('is-glyph');
        thumb.dataset.pdf = item.file_url;
        thumb.appendChild(el('span', 'mono', kindLabel(item.kind)));
      } else {
        thumb.classList.add('is-glyph');
        thumb.appendChild(el('span', 'mono', kindLabel(item.kind)));
      }
      li.appendChild(thumb);

      const body = el('div', 'pres-body');
      body.appendChild(el('div', 'pres-kind mono',
        (idx + 1) + ' · ' + kindLabel(item.kind)));
      body.appendChild(el('div', 'pres-name',
        item.dashboard_title || item.original_name || t('Slide')));
      body.appendChild(metaLine(item, idx));
      if (item.note) body.appendChild(el('div', 'pres-note-view', item.note));

      const editor = el('div', 'pres-note-edit');
      editor.hidden = true;
      const ta = document.createElement('textarea');
      ta.className = 'input';
      ta.rows = 3;
      ta.maxLength = 2000;
      ta.value = item.note || '';
      ta.placeholder = t('Slide note — visible to you only');
      const saveBtn = el('button', 'btn xs', t('Save'));
      saveBtn.type = 'button';
      editor.appendChild(ta);
      editor.appendChild(saveBtn);
      body.appendChild(editor);
      li.appendChild(body);

      const acts = el('div', 'pres-acts');
      const noteBtn = el('button', 'btn xs ghost', item.note ? t('Note ✓') : t('Note'));
      noteBtn.type = 'button';
      noteBtn.addEventListener('click', function () {
        editor.hidden = !editor.hidden;
        if (!editor.hidden) ta.focus();
      });
      saveBtn.addEventListener('click', async function () {
        saveBtn.disabled = true;
        const res = await saveNote(PID, item.id, ta.value || null);
        saveBtn.disabled = false;
        if (!res.ok) { toast(t('Failed to save note'), 'error'); return; }
        toast(t('Note saved'), 'success');
        await reload();
      });
      const delBtn = el('button', 'btn xs ghost', '✕');
      delBtn.type = 'button';
      delBtn.title = t('Remove slide');
      delBtn.addEventListener('click', async function () {
        if (!window.confirm(t('Remove slide from presentation?'))) return;
        const res = await deleteItem(PID, item.id);
        if (!res.ok) { toast(t('Failed to remove slide'), 'error'); return; }
        await reload();
      });
      acts.appendChild(noteBtn);
      acts.appendChild(delBtn);
      li.appendChild(acts);
      return li;
    }

    // First pages of PDF slides as thumbnails — one pdf.js import, small
    // canvases, best-effort (a failure just leaves the glyph).
    let pdfjsMod = null, pdfThumbTimer = null;
    function renderPdfThumbsSoon() { clearTimeout(pdfThumbTimer); pdfThumbTimer = setTimeout(renderPdfThumbs, 0); }
    async function renderPdfThumbs() {
      if (!$list) return;
      const thumbs = Array.from($list.querySelectorAll('.pres-thumb[data-pdf]:not([data-drawn])'));
      if (!thumbs.length) return;
      try { pdfjsMod = pdfjsMod || await import('/static/vendor/pdfjs/pdf.min.mjs'); }
      catch (e) { return; }
      pdfjsMod.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
      for (const tv of thumbs) {
        tv.dataset.drawn = '1';
        try {
          const doc = await pdfjsMod.getDocument({ url: tv.dataset.pdf, withCredentials: true }).promise;
          const page = await doc.getPage(1);
          const base = page.getViewport({ scale: 1 });
          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          const vp = page.getViewport({ scale: Math.max(96 / base.width, 60 / base.height) * dpr });
          const c = document.createElement('canvas');
          c.width = Math.floor(vp.width); c.height = Math.floor(vp.height);
          await page.render({ canvasContext: c.getContext('2d'), viewport: vp }).promise;
          tv.textContent = ''; tv.classList.remove('is-glyph'); tv.appendChild(c);
        } catch (e) { /* keep the glyph */ }
      }
    }
    function render() {
      if (!$list) return;
      renderPdfThumbsSoon();
      $list.textContent = '';
      DATA.items.forEach(function (item, idx) { $list.appendChild(slideNode(item, idx)); });
      if ($empty) $empty.hidden = DATA.items.length > 0;
      if ($count) $count.textContent = String(DATA.items.length);
      if ($show) {
        const url = firstSlideUrl(PID, DATA.items);
        if (url) { $show.href = url; $show.classList.remove('is-off'); }
        else { $show.href = '#'; $show.classList.add('is-off'); }
      }
    }

    async function reload() {
      const res = await fetch(presApiUrl(PID), { headers: { 'Accept': 'application/json' } });
      if (!res.ok) return;
      DATA = await res.json();
      render();
      refreshCheck();
    }

    // ── Freshness, shown in place and on demand ────────────────────
    async function fetchCheck() {
      const res = await fetch(checkUrl(PID), { headers: { 'Accept': 'application/json' } });
      if (!res.ok) return null;
      const body = await res.json();
      CHECK = {};
      (body.slides || []).forEach(function (s) { CHECK[s.position] = s; });
      return body.slides || [];
    }
    async function refreshCheck() {
      if (!DATA.items.length) return;
      const slides = await fetchCheck();
      if (slides) render();
    }

    $checkBtn && $checkBtn.addEventListener('click', async function () {
      $checkBtn.disabled = true;
      const slides = await fetchCheck();
      $checkBtn.disabled = false;
      if (!$checkOut) return;
      $checkOut.textContent = '';
      $checkOut.hidden = false;
      if (!slides || !slides.length) {
        $checkOut.appendChild(el('div', 'pres-check-row', t('Presentation has no slides yet.')));
        return;
      }
      let problems = 0;
      slides.forEach(function (s, i) {
        const row = el('div', 'pres-check-row');
        row.appendChild(el('span', 'mono dim', String(i + 1)));
        row.appendChild(el('span', 'pres-check-name', s.title || kindLabel(s.kind)));
        if (s.access === false) {
          problems++;
          row.appendChild(el('span', 'pres-chip warn', t('no access')));
        } else if (s.kind !== 'dashboard') {
          row.appendChild(el('span', 'dim', kindLabel(s.kind)));
        } else {
          if (s.pub_at) row.appendChild(el('span', 'dim', t('publication {p1}', {p1: s.pub_at})));
          if (s.data_at) row.appendChild(el('span', 'dim', t('· data as of {p1}', {p1: s.data_at})));
          if (s.stale) { problems++; row.appendChild(el('span', 'pres-chip warn', t('outdated'))); }
        }
        $checkOut.appendChild(row);
      });
      const sum = el('div', 'pres-check-sum');
      sum.textContent = problems
        ? t('Need attention: {p1} of {p2}', {p1: problems, p2: slides.length})
        : t('All slides are up to date — ready to present.');
      if (problems) sum.classList.add('warn');
      $checkOut.appendChild(sum);
      render();
    });

    // ── Save / delete ─────────────────────────────────────────────
    // Everything persists as it happens (title on blur, order on drop,
    // notes on OK). "Save" commits the title field and re-PUTs the
    // order, then stamps the time — the visible reassurance people asked
    // for. "Delete" removes the deck and its files after a confirm.
    function stampSaved() {
      const el = document.getElementById('pres-saved');
      if (!el) return;
      const d = new Date();
      el.textContent = t('Saved {p1}:{p2}', {p1: String(d.getHours()).padStart(2, '0'), p2: String(d.getMinutes()).padStart(2, '0')});
    }
    let commitTitle = null;
    const $saveBtn = document.getElementById('pres-save-btn');
    $saveBtn && $saveBtn.addEventListener('click', async function () {
      $saveBtn.disabled = true;
      try {
        if (commitTitle) await commitTitle();
        const ids = DATA.items.map(function (it) { return it.id; });
        const res = ids.length ? await saveOrder(PID, ids) : { ok: true };
        if (!res.ok) { toast(t('Failed to save order'), 'error'); return; }
        stampSaved();
        toast(t('Saved'), 'success');
      } finally { $saveBtn.disabled = false; }
    });
    const $deleteBtn = document.getElementById('pres-delete-btn');
    $deleteBtn && $deleteBtn.addEventListener('click', async function () {
      const n = DATA.items.length;
      if (!confirm(t('Delete presentation{p1}? Reports stay, only uploaded images and PDFs are removed.', {p1: (n ? t(' and {p1} slide(s)', {p1: n}) : '')}))) return;
      $deleteBtn.disabled = true;
      const res = await fetch(presApiUrl(PID), jsonRequest('DELETE'));
      if (!res.ok) { $deleteBtn.disabled = false; toast(t('Failed to delete'), 'error'); return; }
      location.href = '/presentations';
    });

    // ── Title ─────────────────────────────────────────────────────
    if ($titleInput) {
      let last = $titleInput.value;
      const commit = async function () {
        const v = ($titleInput.value || '').trim();
        if (!v || v === last) { $titleInput.value = last; return; }
        const res = await saveTitle(PID, v);
        if (!res.ok) { $titleInput.value = last; toast(t('Rename failed'), 'error'); return; }
        last = v;
        const crumb = document.getElementById('pres-crumb');
        if (crumb) crumb.textContent = v;
        toast(t('Name saved'), 'success');
      };
      commitTitle = commit;
      $titleInput.addEventListener('blur', commit);
      $titleInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); $titleInput.blur(); }
        if (e.key === 'Escape') { $titleInput.value = last; $titleInput.blur(); }
      });
    }

    // ── Drag and drop order (native HTML5) ────────────────────────
    let dragEl = null;
    if ($list) {
      $list.addEventListener('dragstart', function (e) {
        const row = e.target.closest ? e.target.closest('.pres-slide') : null;
        if (!row) return;
        dragEl = row;
        row.classList.add('dragging');
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', row.dataset.id);
        }
      });
      $list.addEventListener('dragover', function (e) {
        if (!dragEl) return;
        e.preventDefault();
        const over = e.target.closest ? e.target.closest('.pres-slide') : null;
        if (!over || over === dragEl) return;
        const r = over.getBoundingClientRect();
        const after = (e.clientY - r.top) > r.height / 2;
        $list.insertBefore(dragEl, after ? over.nextSibling : over);
      });
      // `drop` is the only event a COMPLETED drag fires; Esc or dropping
      // outside the list fires `dragend` alone. The live DOM already shows
      // the tentative order, so a cancelled drag must be reverted, not saved.
      let dropped = false;
      $list.addEventListener('drop', function (e) {
        if (!dragEl) return;
        e.preventDefault();
        dropped = true;
      });
      $list.addEventListener('dragend', async function () {
        if (!dragEl) return;
        dragEl.classList.remove('dragging');
        dragEl = null;
        const completed = dropped;
        dropped = false;
        if (!completed) { render(); return; }  // cancelled: redraw the saved order
        const ids = orderIdsFrom($list, '.pres-slide');
        const res = await saveOrder(PID, ids);
        if (!res.ok) { toast(t('Failed to save order'), 'error'); }
        await reload();
      });
    }

    // ── Add: report picker ────────────────────────────────────────
    let PICKABLE = null;
    function renderPicker(q) {
      if (!$pickerList) return;
      $pickerList.textContent = '';
      const needle = (q || '').toLowerCase();
      const rows = (PICKABLE || []).filter(function (d) {
        return !needle || (d.title || '').toLowerCase().indexOf(needle) >= 0;
      });
      if (!rows.length) {
        $pickerList.appendChild(el('div', 'pres-pick-empty', t('Nothing found')));
        return;
      }
      rows.forEach(function (d) {
        const b = el('button', 'pres-pick');
        b.type = 'button';
        if (d.fav != null) b.appendChild(el('span', 'pres-star', '★'));
        b.appendChild(el('span', 'pres-pick-name', d.title || d.id));
        b.addEventListener('click', async function () {
          b.disabled = true;
          const res = await addDashboard(PID, d.id);
          b.disabled = false;
          if (!res.ok) { toast(t('Report not added'), 'error'); return; }
          closePicker();
          await reload();
        });
        $pickerList.appendChild(b);
      });
    }
    function closePicker() { if ($picker) $picker.hidden = true; }
    document.getElementById('pres-add-dash')?.addEventListener('click', async function () {
      if (!$picker) return;
      $picker.hidden = false;
      if (PICKABLE === null) {
        const res = await fetch(PICKABLE_URL, { headers: { 'Accept': 'application/json' } });
        PICKABLE = res.ok ? await res.json() : [];
      }
      renderPicker($pickerSearch ? $pickerSearch.value : '');
      $pickerSearch && $pickerSearch.focus();
    });
    $pickerSearch && $pickerSearch.addEventListener('input', function () {
      renderPicker($pickerSearch.value);
    });
    document.getElementById('pres-picker-close')?.addEventListener('click', closePicker);

    // ── Add: image / pdf ──────────────────────────────────────────
    function pickFile(accept) {
      if (!$file) return;
      $file.value = '';
      $file.accept = accept;
      $file.click();
    }
    document.getElementById('pres-add-image')?.addEventListener('click', function () {
      pickFile('image/png,image/jpeg');
    });
    document.getElementById('pres-add-pdf')?.addEventListener('click', function () {
      pickFile('application/pdf');
    });
    document.getElementById('pres-add-html')?.addEventListener('click', function () {
      pickFile('text/html,.html,.htm');
    });
    // pdf-lib loads lazily on the first PDF that actually needs splitting.
    let pdfLibPromise = null;
    function loadPdfLib() {
      if (window.PDFLib) return Promise.resolve(window.PDFLib);
      if (!pdfLibPromise) {
        pdfLibPromise = new Promise(function (resolve, reject) {
          const s = document.createElement('script');
          s.src = '/static/vendor/pdf-lib.min.js';
          s.onload = function () { resolve(window.PDFLib); };
          s.onerror = function () { pdfLibPromise = null; reject(new Error(t('pdf-lib failed to load'))); };
          document.head.appendChild(s);
        });
      }
      return pdfLibPromise;
    }

    async function uploadOne(f) {
      const res = await uploadSlide(PID, f);
      if (!res.ok) {
        let msg = t('File rejected');
        try { const b = await res.json(); if (b && b.detail) msg = b.detail; } catch (e) {}
        throw new Error(msg);
      }
    }

    async function uploadPdfSplit(f) {
      toast(t('Splitting PDF into pages…'));
      const PDFLib = await loadPdfLib();
      const pages = await splitPdf(PDFLib, await f.arrayBuffer());
      if (pages.length <= 1) { await uploadOne(f); return; }
      for (let i = 0; i < pages.length; i++) {
        const name = pdfPageName(f.name, i + 1, pages.length);
        toast(t('Page {p1} of {p2}…', {p1: (i + 1), p2: pages.length}));
        await uploadOne(new File([pages[i]], name, { type: 'application/pdf' }));
      }
      toast(t('Slides added: {p1}', {p1: pages.length}));
    }

    $file && $file.addEventListener('change', async function () {
      const f = $file.files && $file.files[0];
      if (!f) return;
      const isPdf = /\.pdf$/i.test(f.name) || f.type === 'application/pdf';
      const $split = document.getElementById('pres-pdf-split');
      try {
        if (isPdf && $split && $split.checked) {
          await uploadPdfSplit(f);
        } else {
          await uploadOne(f);
        }
      } catch (err) {
        toast(err && err.message ? err.message : t('File rejected'), 'error');
        // A split that failed on page k has already committed pages 1..k-1:
        // show them, so the retry is «add the rest», not a duplicate deck.
        $file.value = '';
        await reload();
        return;
      }
      $file.value = '';
      await reload();
    });

    render();
    refreshCheck();
  })();

  // ═════════════════════════════════════════════════════════════════
  // "+ build a presentation" — works anywhere the button is rendered
  // ═════════════════════════════════════════════════════════════════

  // Delegated: the home grid creates its tile lazily, after this script ran.
  document.addEventListener('click', async function (e) {
    const btn = e.target.closest ? e.target.closest('[data-pres-create]') : null;
    if (!btn) return;
    {
      e.preventDefault();
      btn.disabled = true;
      const res = await createPresentation(t('New presentation'));
      btn.disabled = false;
      if (!res.ok) { toast(t('Presentation creation failed'), 'error'); return; }
      const body = await res.json();
      window.location.href = builderUrl(body.id);
    }
  });
})();
