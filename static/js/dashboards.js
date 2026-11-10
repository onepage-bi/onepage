/* OnePage — dashboards index: view toggle, filters, search, iframe previews,
   shared folders (collapse / create / rename / delete / move-to-folder),
   "My reports" favourites (star toggle + own drag-and-drop order).
   Grid cards are built lazily from the #dash-data JSON blob on first grid
   activation — the server ships only the table DOM. */
(function() {
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // ═════════════════════════════════════════════════════════════════
  // Pure helpers (exported for node tests — no DOM/window touched here)
  // ═════════════════════════════════════════════════════════════════

  // Splits the item list into the user's own favourites (sorted by their own
  // `position`) and everything else, in original order. Presentations are a
  // separate top-level payload key (`presentations`), not part of `items`.
  function sectionize(items) {
    const list = items || [];
    const favorites = list.filter(d => d.fav != null).slice().sort((a, b) => a.fav - b.fav);
    const others = list.filter(d => d.fav == null);
    return { favorites, others };
  }

  const STALE_DAYS = 7;
  // `dateStr`/`today` are "YYYY-MM-DD". `today` is injectable for tests;
  // defaults to the real date so callers in the page never have to pass it.
  function isStale(dateStr, today) {
    if (!dateStr) return false;
    const d = Date.parse(dateStr + 'T00:00:00Z');
    const tv = Date.parse((today || new Date().toISOString().slice(0, 10)) + 'T00:00:00Z');
    if (isNaN(d) || isNaN(tv)) return false;
    return Math.round((tv - d) / 86400000) > STALE_DAYS;
  }

  // "YYYY-MM-DD" -> "DD.MM" for the compact foot-row label.
  function fmtDM(dateStr) {
    if (!dateStr) return null;
    const parts = String(dateStr).split('-');
    return parts.length === 3 ? parts[2] + '.' + parts[1] : dateStr;
  }

  function favoriteUrl(id) { return '/api/dashboards/' + encodeURIComponent(id) + '/favorite'; }
  const FAVORITES_ORDER_URL = '/api/favorites/order';

  // OriginGuard requires Content-Type on every non-GET request — both calls
  // below set it explicitly even though PUT/DELETE carry no body here.
  async function setFavorite(id, on) {
    const res = await fetch(favoriteUrl(id), {
      method: on ? 'PUT' : 'DELETE',
      headers: { 'Content-Type': 'application/json' },
    });
    return res.ok;
  }

  async function saveFavoritesOrder(ids) {
    const res = await fetch(FAVORITES_ORDER_URL, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    return res.ok;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { sectionize, isStale, fmtDM, favoriteUrl, setFavorite, saveFavoritesOrder };
  }
  if (typeof document === 'undefined') return; // node require() path stops here

  // ── Data (shared by grid builder + move popover) ────────────────────
  let ITEMS = [], FOLDERS = [], PRESENTATIONS = [], PREVIEW_MAX = 1500000;
  try {
    const payload = JSON.parse(document.getElementById('dash-data')?.textContent || '{}');
    ITEMS = payload.items || [];
    FOLDERS = payload.folders || [];
    PRESENTATIONS = payload.presentations || [];
    if (payload.preview_max != null) PREVIEW_MAX = payload.preview_max; // 0 = cutoff disabled
  } catch (e) { /* keep empty */ }

  const TODAY = new Date().toISOString().slice(0, 10);
  const U = window.OP_USER || {};
  const canManage = d => U.role === 'administrator' || (U.role === 'superuser' && d.owner === U.person_id);

  // ── Collapsed folders (persisted) ───────────────────────────────────
  let collapsed = new Set();
  try { collapsed = new Set(JSON.parse(localStorage.getItem('op-dash-folders-collapsed') || '[]')); } catch (e) {}
  function saveCollapsed() {
    try { localStorage.setItem('op-dash-folders-collapsed', JSON.stringify([...collapsed])); } catch (e) {}
  }
  function toggleCollapse(fid) {
    collapsed.has(fid) ? collapsed.delete(fid) : collapsed.add(fid);
    saveCollapsed();
    apply();
  }

  // ── Filters + search ────────────────────────────────────────────────
  const filters = { type: null, status: null, owner: null };
  let q = '';
  const filterActive = () => !!(q || filters.type || filters.status || filters.owner);

  document.querySelectorAll('#dash-filters .chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const k = btn.dataset.filter;
      const v = btn.dataset.val;
      filters[k] = filters[k] === v ? null : v;
      document.querySelectorAll(`#dash-filters .chip[data-filter="${k}"]`)
        .forEach(b => b.setAttribute('aria-pressed', filters[k] === b.dataset.val));
      apply();
    });
  });
  document.getElementById('dash-search')?.addEventListener('input', e => {
    q = e.target.value.toLowerCase();
    apply();
  });

  function matches(el) {
    if (filters.type && el.dataset.type !== filters.type) return false;
    if (filters.status && el.dataset.status !== filters.status) return false;
    if (filters.owner && el.dataset.owner !== filters.owner) return false;
    if (q && !(el.dataset.search || '').includes(q)) return false;
    return true;
  }

  // Visibility pass over table rows + grid cards + both kinds of folder
  // headers. While a filter/search is active, collapsed folders auto-expand
  // and headers with zero hits hide; idle state restores the collapse.
  // Favourites/presentations/"Others" section rows carry their own class
  // (not .folder-row), so they never enter this pass.
  function apply() {
    const active = filterActive();
    let shown = 0;
    const perFolder = {};
    document.querySelectorAll('#dash-tbody tr[data-id]').forEach(r => {
      const m = matches(r);
      const fid = r.dataset.folder || '';
      r.style.display = (m && (active || !collapsed.has(fid))) ? '' : 'none';
      if (m) { shown++; perFolder[fid] = (perFolder[fid] || 0) + 1; }
    });
    const perFolderGrid = {};
    document.querySelectorAll('#dash-grid .dash-card').forEach(c => {
      const fid = c.dataset.folder || '';
      const m = matches(c);
      c.style.display = (m && (active || !collapsed.has(fid))) ? '' : 'none';
      if (m) perFolderGrid[fid] = (perFolderGrid[fid] || 0) + 1;
    });
    const favWrap = document.getElementById('dash-fav-list');
    if (favWrap) favWrap.style.display = (!active && collapsed.has('__fav__')) ? 'none' : '';
    document.querySelectorAll('#dash-tbody .folder-row, #dash-grid .folder-sec').forEach(h => {
      const fid = h.dataset.folderId || '';
      const n = h.closest('#dash-grid') ? (perFolderGrid[fid] || 0) : (perFolder[fid] || 0);
      h.style.display = (active && n === 0) ? 'none' : '';
      const c = h.querySelector('[data-folder-count]');
      if (c) c.textContent = n;
      h.classList.toggle('collapsed', !active && collapsed.has(fid));
    });
    // Section rows/heads that are not .folder-sec/.folder-row (table fav
    // head, "Others" in both views): keep their counts live too.
    const favN = ITEMS.filter(i => i.fav != null).length;
    document.querySelectorAll('#dash-tbody tr[data-fav-head] .count').forEach(c => { c.textContent = favN; });
    document.querySelectorAll('[data-others-head] .count').forEach(c => { c.textContent = ITEMS.length - favN; });
    const count = document.getElementById('dash-count');
    if (count) count.textContent = t('{n} shown', {n: shown});
  }

  // ── Favourite star (shared by table + grid, delegated) ──────────────
  function footDatesHtml(d) {
    const pub = fmtDM(d.pub_at);
    if (!pub) return '';
    const stale = isStale(d.data_at, TODAY);
    let html = t('<span class="sep">·</span><span class="mono dash-dates{p1}">published {p2}', {p1: (stale ? ' is-stale' : ''), p2: esc(pub)});
    if (d.data_at) html += t(' · data as of {p1}', {p1: esc(fmtDM(d.data_at))});
    html += '</span>';
    return html;
  }
  function starBtn(d) {
    const on = d.fav != null;
    return '<button class="card-fav" data-fav-id="' + esc(d.id) + '" aria-pressed="' + on +
      '" title="' + (on ? t('Remove from favourites') : t('Add to favourites')) + '">' + (on ? '★' : '☆') + '</button>';
  }
  function syncFavButtons() {
    ITEMS.forEach(d => {
      const on = d.fav != null;
      document.querySelectorAll('.card-fav[data-fav-id="' + d.id + '"]').forEach(b => {
        b.setAttribute('aria-pressed', String(on));
        b.textContent = on ? '★' : '☆';
        b.title = on ? t('Remove from favourites') : t('Add to favourites');
      });
    });
  }
  // Where an un-starred element goes back to: after its folder's own header
  // (table `.folder-row`, grid `.folder-sec`), else after the "Others"
  // head. `data-folder` is restored from ITEMS, so folder collapse and the
  // per-folder counts see it again.
  function realFolder(item) { return item && item.folder != null ? String(item.folder) : ''; }
  function returnToFolder(el, fid, headSel, othersSel) {
    const head = document.querySelector(headSel.replace('%f', fid)) || document.querySelector(othersSel);
    if (head) head.after(el);
  }
  // The first star on a page that loaded without favourites: create the
  // section on the fly (header + list on the grid, header row in the
  // table), so the card visibly moves instead of staying put with a star.
  function ensureGridFavSection() {
    let favWrap = document.getElementById('dash-fav-list');
    const grid = document.getElementById('dash-grid');
    if (favWrap || !grid || !gridBuilt) return favWrap;
    const head = gridFolderHead('__fav__', t('Favorites'), 0, '#ic-star');
    favWrap = document.createElement('div');
    favWrap.className = 'dash-fav-list';
    favWrap.id = 'dash-fav-list';
    if (!grid.querySelector('[data-others-head]')) {
      const othersHead = sectionHead(t('Other'), ITEMS.length);
      othersHead.setAttribute('data-others-head', '1');
      grid.prepend(othersHead);
    }
    grid.prepend(head, favWrap);
    wireFavDragDrop(favWrap, '.dash-card');
    return favWrap;
  }
  function ensureTableFavSection() {
    const tbody = document.getElementById('dash-tbody');
    if (!tbody || tbody.querySelector('tr[data-fav-head]')) return;
    const frag = document.createDocumentFragment();
    frag.appendChild(tableSectionRow(t('★ Favorites'), 0, { 'data-fav-head': '1' }));
    if (!tbody.querySelector('tr[data-others-head]')) {
      frag.appendChild(tableSectionRow(t('Other'), ITEMS.length, { 'data-others-head': '1' }));
    }
    tbody.insertBefore(frag, tbody.firstChild);
    wireFavDragDrop(tbody, 'tr.fav-row');
  }
  function updateFavState(id, on) {
    const item = ITEMS.find(i => i.id === id);
    if (item) item.fav = on ? (Math.max(0, ...ITEMS.map(i => i.fav || 0)) + 1) : null;
    document.querySelectorAll('.card-fav[data-fav-id="' + id + '"]').forEach(b => {
      b.setAttribute('aria-pressed', String(on));
      b.textContent = on ? '★' : '☆';
      b.title = on ? t('Remove from favourites') : t('Add to favourites');
    });
    const fid = realFolder(item);
    const card = document.querySelector('#dash-grid .dash-card[data-id="' + id + '"]');
    const favList = on ? ensureGridFavSection() : document.getElementById('dash-fav-list');
    if (on) ensureTableFavSection();
    if (card) {
      card.draggable = on;
      card.dataset.folder = on ? '__fav__' : fid; // '__fav__' is never a collapsed key
      if (on && favList) favList.appendChild(card);
      else if (!on) returnToFolder(card, fid, '#dash-grid .folder-sec[data-folder-id="%f"]', '#dash-grid [data-others-head]');
      if (!on) card.style.display = '';
    }
    const row = document.querySelector('#dash-tbody tr[data-id="' + id + '"]');
    if (row) {
      row.classList.toggle('fav-row', on);
      row.draggable = on;
      row.dataset.folder = on ? '__fav__' : fid;
      if (on) {
        const favHead = document.querySelector('#dash-tbody tr[data-fav-head]');
        if (favHead) favHead.after(row);
      } else {
        returnToFolder(row, fid, '#dash-tbody tr.folder-row[data-folder-id="%f"]', '#dash-tbody tr[data-others-head]');
      }
    }
    apply();
  }
  document.addEventListener('click', async e => {
    const btn = e.target.closest('.card-fav');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    if (btn.disabled) return;
    const id = btn.dataset.favId;
    const turningOn = btn.getAttribute('aria-pressed') !== 'true';
    btn.disabled = true;
    const ok = await setFavorite(id, turningOn);
    btn.disabled = false;
    if (ok) updateFavState(id, turningOn);
  });

  // Native HTML5 drag-and-drop, scoped to `itemSelector` inside `container`
  // (the favourites section only) — persists the new order on drop.
  function wireFavDragDrop(container, itemSelector) {
    let dragEl = null;
    container.addEventListener('dragstart', e => {
      const el = e.target.closest(itemSelector);
      if (!el) return;
      dragEl = el;
      try { e.dataTransfer.effectAllowed = 'move'; } catch (err) {}
    });
    container.addEventListener('dragover', e => {
      if (!dragEl) return;
      const over = e.target.closest(itemSelector);
      if (!over || over === dragEl) return;
      e.preventDefault();
      const rect = over.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      over.parentNode.insertBefore(dragEl, before ? over : over.nextSibling);
    });
    container.addEventListener('drop', e => { if (dragEl) e.preventDefault(); });
    container.addEventListener('dragend', async () => {
      if (!dragEl) { return; }
      dragEl = null;
      const ids = Array.from(container.querySelectorAll(itemSelector)).map(el => el.dataset.id);
      await saveFavoritesOrder(ids);
    });
  }

  // ── Grid builder (runs once) ────────────────────────────────────────
  let gridBuilt = false;
  const FOLDER_HEAD_HTML =
    '<svg class="ico ico-12 chev"><use href="#ic-chevron-down"/></svg>' +
    '<svg class="ico ico-12 folder-ico"><use href="#ic-folder"/></svg>';
  function fmtMB(bytes) {
    return (bytes / 1048576).toFixed(1) + ' MB';
  }
  // A collapsible grid header for a synthetic folder (favourites,
  // presentations): same .folder-sec as real folders, so apply() counts,
  // collapses and filters it through the one visibility pass.
  function gridFolderHead(key, title, count, icon) {
    const h = document.createElement('div');
    h.className = 'folder-sec';
    h.dataset.folderId = key;
    h.innerHTML = '<svg class="ico ico-12 chev"><use href="#ic-chevron-down"/></svg>' +
      '<svg class="ico ico-12 folder-ico"><use href="' + icon + '"/></svg>' +
      '<span class="folder-name">' + esc(title) + '</span>' +
      '<span class="count mono" data-folder-count>' + count + '</span>';
    h.addEventListener('click', () => toggleCollapse(key));
    return h;
  }
  function sectionHead(title, count) {
    const h = document.createElement('div');
    h.className = 'dash-section-head';
    h.innerHTML = '<span class="dash-section-title">' + esc(title) + '</span>' +
      '<span class="count mono">' + count + '</span>';
    return h;
  }
  function cardFor(d) {
    const a = document.createElement('a');
    a.className = 'dash-card';
    a.href = '/d/' + d.id;
    a.dataset.id = d.id;
    a.dataset.folder = d.folder == null ? '' : String(d.folder);
    a.dataset.type = d.type;
    a.dataset.status = d.pub ? 'published' : 'draft';
    a.dataset.owner = d.owner;
    a.dataset.search = (d.title + ' ' + d.desc + ' ' + d.owner).toLowerCase();
    if (d.fav != null) a.draggable = true;
    // Heavy dashboards get a zero-byte placeholder instead of a live iframe —
    // the card link still opens the full viewer.
    // "Strictly confidential" loses the live preview for the same reason
    // a heavy report does: there will be no frame. Only the cause differs —
    // /render/{id}/ answers 403 until the password is entered, and an empty frame
    // instead of a tile would mean "the report is broken", not "the report is locked".
    const heavy = (PREVIEW_MAX > 0 && (d.size || 0) > PREVIEW_MAX) || !!d.conf;
    if (heavy) a.classList.add('is-tile', 'tint-' + tintOf(d.id));
    const previewHtml = heavy
      ? tileHtml(tintOf(d.id), TILE_REPORT)
      : '<div class="preview"><iframe class="preview-frame" data-src="/render/' + d.id + '/"' +
        ' scrolling="no" tabindex="-1" style="position:absolute;top:0;left:0;width:1280px;height:800px;' +
        'transform-origin:top left;border:none;pointer-events:none;opacity:0;transition:opacity .4s"></iframe></div>';
    a.innerHTML = previewHtml +
      '<div class="meta-row">' + starBtn(d) +
      (d.conf
        ? '<span class="badge tile-chip">' + t('password-protected') + '</span>'
        : heavy
        ? '<span class="badge tile-chip">' + t('large report · {size}', {size: fmtMB(d.size)}) + '</span>'
        : '<span class="badge type-' + esc(d.type) + '">' + esc(d.type) + '</span>') +
      (d.pub ? '' : '<span class="badge draft">' + t('draft') + '</span>') +
      (FOLDERS.length && canManage(d)
        ? '<span class="grow"></span><button class="btn icon xs ghost card-move" data-id="' + d.id +
          '" title="' + t('Move to folder') + '"><svg class="ico ico-12"><use href="#ic-folder"/></svg></button>'
        : '') + '</div>' +
      '<div class="name-row">' + esc(d.title) + '</div>' +
      (d.desc ? '<div class="desc-row">' + esc(d.desc) + '</div>' : '') +
      '<div class="foot-row"><span class="mono">' + (esc(d.owner) || '—') + '</span>' +
      '<span class="sep">·</span><span class="mono">v' + d.ver + '</span>' +
      footDatesHtml(d) + '</div>' +
      (heavy ? t('<div class="act-row"><span class="btn xs">Open report</span></div>') : '');
    return a;
  }
  // ── Tiles: the cards that have no live preview ──────────────────────
  // A heavy report and a presentation get a tinted tile with a glyph and
  // a soft skyline instead of grey-on-grey text. The tint is stable per id
  // (a hash), so a card keeps its colour between visits.
  const TINTS = ['blue', 'violet', 'green', 'amber', 'teal'];
  function tintOf(id) {
    let h = 0;
    const str = String(id || '');
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
    return TINTS[h % TINTS.length];
  }
  // A document with a little bar chart on it.
  const TILE_REPORT =
    '<svg class="tile-glyph" viewBox="0 0 64 72" aria-hidden="true">' +
    '<path class="g-paper" d="M10 4h30l14 14v46a4 4 0 0 1-4 4H10a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4z"/>' +
    '<path class="g-fold" d="M40 4v14h14"/>' +
    '<rect class="g-bar" x="16" y="44" width="7" height="14" rx="1.5"/>' +
    '<rect class="g-bar" x="28" y="34" width="7" height="24" rx="1.5"/>' +
    '<rect class="g-bar" x="40" y="40" width="7" height="18" rx="1.5"/>' +
    '<path class="g-line" d="M16 30l11-8 9 4 12-10"/>' +
    '</svg>';
  // A stack of slides.
  const TILE_DECK =
    '<svg class="tile-glyph" viewBox="0 0 72 64" aria-hidden="true">' +
    '<rect class="g-back" x="14" y="4" width="50" height="34" rx="4"/>' +
    '<rect class="g-back" x="9" y="10" width="50" height="34" rx="4"/>' +
    '<rect class="g-paper" x="4" y="16" width="50" height="34" rx="4"/>' +
    '<rect class="g-bar" x="11" y="24" width="18" height="4" rx="2"/>' +
    '<rect class="g-bar" x="11" y="33" width="30" height="3" rx="1.5"/>' +
    '<rect class="g-bar" x="11" y="40" width="24" height="3" rx="1.5"/>' +
    '<path class="g-play" d="M58 48l10 6-10 6z"/>' +
    '</svg>';
  function tileHtml(tint, glyph) {
    return '<div class="preview tile tile-' + tint + '">' +
      '<span class="tile-expand"><svg class="ico ico-12"><use href="#ic-expand"/></svg></span>' +
      glyph + '<div class="tile-skyline"></div></div>';
  }
  // A presentation card links to its builder — decks have no preview iframe,
  // the tile is the deck's title + slide count + last-touched date.
  function presCardFor(p) {
    const a = document.createElement('a');
    a.className = 'dash-card pres-card is-tile tint-' + tintOf('pres:' + p.id);
    a.href = '/presentations/' + encodeURIComponent(p.id);
    a.dataset.search = String(p.title || '').toLowerCase();
    const n = p.items_count || 0;
    const slides = tn(n, '{n} slide|{n} slides');
    a.innerHTML =
      t('{p1}<div class="meta-row"><span class="badge tile-chip">presentation · {p2}</span></div><div class="name-row">{p3}</div><div class="foot-row"><span class="mono">{p4}</span></div><div class="act-row">{p5}<span class="btn xs">Create</span></div>', {p1: tileHtml(tintOf('pres:' + p.id), TILE_DECK), p2: slides, p3: esc(p.title), p4: esc(p.updated_at || ''), p5: (n ? t('<span class="btn xs primary" data-pres-show="/presentations/{p1}/show?i=0"><svg class="ico ico-12"><use href="#ic-play"/></svg> Show</span>', {p1: encodeURIComponent(p.id)}) : '')});
    return a;
  }
  // "Show" on a deck tile: the tile is a link to the builder, the
  // button goes to the first slide instead.
  document.addEventListener('click', e => {
    const show = e.target.closest ? e.target.closest('[data-pres-show]') : null;
    if (!show) return;
    e.preventDefault(); e.stopPropagation();
    location.href = show.dataset.presShow;
  });
  function presCreateCard() {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'dash-card pres-card pres-create is-tile';
    b.setAttribute('data-pres-create', '1');
    // The glyph is markup, not language: it goes in as a template and the two
    // text rows are filled through textContent, so t() only ever sees a whole
    // phrase (and no translation can inject HTML here).
    b.innerHTML = '<div class="preview tile tile-none"><svg class="tile-glyph" viewBox="0 0 64 64" aria-hidden="true"><rect class="g-paper" x="8" y="12" width="48" height="40" rx="5"/><path class="g-plus" d="M32 22v20M22 32h20"/></svg></div><div class="name-row"></div><div class="desc-row"></div>';
    b.querySelector('.name-row').textContent = t('Build a presentation');
    b.querySelector('.desc-row').textContent = t('reports, images and PDFs in one show');
    return b;
  }
  function buildGrid() {
    if (gridBuilt) return;
    gridBuilt = true;
    const grid = document.getElementById('dash-grid');
    if (!grid || !ITEMS.length && !FOLDERS.length) return;

    const { favorites, others } = sectionize(ITEMS);
    const frag = document.createDocumentFragment();

    // Favourites and presentations are folders like any other: a full-width
    // collapsible header (chevron + icon + count), cards underneath. Their
    // keys ('__fav__', '__pres__') can never collide with a real folder id.
    if (favorites.length) {
      frag.appendChild(gridFolderHead('__fav__', t('Favorites'), favorites.length, '#ic-star'));
      const favWrap = document.createElement('div');
      favWrap.className = 'dash-fav-list';
      favWrap.id = 'dash-fav-list';
      favorites.forEach(d => { const c = cardFor(d); c.dataset.folder = '__fav__'; favWrap.appendChild(c); });
      frag.appendChild(favWrap);
      wireFavDragDrop(favWrap, '.dash-card');
    }
    if (PRESENTATIONS.length) {
      frag.appendChild(gridFolderHead('__pres__', t('Presentations'), PRESENTATIONS.length, '#ic-folder'));
      PRESENTATIONS.forEach(p => { const c = presCardFor(p); c.dataset.folder = '__pres__'; frag.appendChild(c); });
      const create = presCreateCard(); create.dataset.folder = '__pres__'; frag.appendChild(create);
    }
    if (favorites.length || PRESENTATIONS.length) {
      const othersHead = sectionHead(t('Other'), others.length);
      othersHead.setAttribute('data-others-head', '1');
      frag.appendChild(othersHead);
    }

    const byFolder = { '': [] };
    FOLDERS.forEach(f => { byFolder[String(f.id)] = []; });
    for (const d of others) {
      const k = d.folder == null ? '' : String(d.folder);
      (byFolder[k] || byFolder['']).push(d);
    }

    const sections = [['', t('Unsorted')], ...FOLDERS.map(f => [String(f.id), f.name])];
    for (const [fid, name] of sections) {
      const list = byFolder[fid] || [];
      const needHeader = fid === '' ? (list.length > 0 && FOLDERS.length > 0) : true;
      if (needHeader) {
        const h = document.createElement('div');
        h.className = 'folder-sec';
        h.dataset.folderId = fid;
        const canFolders = U.role === 'administrator' || U.role === 'superuser';
        h.innerHTML = FOLDER_HEAD_HTML +
          '<span class="folder-name">' + esc(name) + '</span>' +
          '<span class="count mono" data-folder-count>' + list.length + '</span>' +
          (fid !== '' && canFolders
            ? '<span class="folder-actions row-actions">' +
              '<button class="btn icon xs ghost folder-rename" data-fid="' + fid + '" data-name="' + esc(name) +
              '" title="' + t('Rename folder') + '"><svg class="ico ico-12"><use href="#ic-settings"/></svg></button>' +
              '<button class="btn icon xs ghost folder-delete" data-fid="' + fid + '" data-name="' + esc(name) +
              '" title="' + t('Delete folder (dashboards move to Unsorted)') + '"><svg class="ico ico-12"><use href="#ic-x"/></svg></button>' +
              '</span>'
            : '');
        h.addEventListener('click', e => {
          if (e.target.closest('.folder-actions')) return; // buttons, not collapse
          toggleCollapse(fid);
        });
        frag.appendChild(h);
      }
      list.forEach(d => frag.appendChild(cardFor(d)));
    }
    grid.appendChild(frag);

    grid.addEventListener('click', async e => {
      const mv = e.target.closest('.card-move');
      if (mv) {
        e.preventDefault();
        e.stopPropagation();
        openMovePopover(mv, mv.dataset.id);
        return;
      }
      await folderAction(e);
    });
  }

  // ── Table sections (favourites / presentations / "Others") ──────────
  // The table is server-rendered folder-by-folder; this reorganizes it once
  // on load without touching the folder markup the folders feature owns.
  function tableSectionRow(title, count, extraAttrs) {
    const tr = document.createElement('tr');
    tr.className = 'dash-section-row';
    if (extraAttrs) Object.keys(extraAttrs).forEach(k => tr.setAttribute(k, extraAttrs[k]));
    const td = document.createElement('td');
    td.setAttribute('colspan', '8');
    td.innerHTML = '<div class="folder-head"><span class="dash-section-title">' + esc(title) + '</span>' +
      '<span class="count mono">' + count + '</span></div>';
    tr.appendChild(td);
    return tr;
  }
  function initTableSections() {
    const tbody = document.getElementById('dash-tbody');
    if (!tbody) return;
    const { favorites, others } = sectionize(ITEMS);
    if (!favorites.length && !PRESENTATIONS.length) return; // nothing to reorganize

    const rowsById = {};
    tbody.querySelectorAll('tr[data-id]').forEach(r => { rowsById[r.dataset.id] = r; });
    const frag = document.createDocumentFragment();

    if (favorites.length) {
      frag.appendChild(tableSectionRow(t('★ Favorites'), favorites.length, { 'data-fav-head': '1' }));
      favorites.forEach(d => {
        const row = rowsById[d.id];
        if (!row) return;
        row.classList.add('fav-row');
        row.draggable = true;
        row.dataset.folder = '__fav__'; // never matches a collapsed real folder
        frag.appendChild(row);
      });
    }
    if (PRESENTATIONS.length) {
      frag.appendChild(tableSectionRow(t('Presentations'), PRESENTATIONS.length, {}));
    }
    frag.appendChild(tableSectionRow(t('Other'), others.length, { 'data-others-head': '1' }));
    tbody.insertBefore(frag, tbody.firstChild);
    wireFavDragDrop(tbody, 'tr.fav-row');
  }

  // ── View toggle ─────────────────────────────────────────────────────
  const tableWrap = document.getElementById('dash-table-wrap');
  const gridWrap  = document.getElementById('dash-grid-wrap');
  document.querySelectorAll('[data-view-btn]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-view-btn]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const view = btn.dataset.viewBtn;
      if (tableWrap) tableWrap.hidden = view !== 'table';
      if (gridWrap)  gridWrap.hidden  = view !== 'grid';
      try { localStorage.setItem('op-dash-view', view); } catch (e) {}
      if (view === 'grid') {
        buildGrid();
        apply();
        initGridPreviews();
      }
    });
  });
  const savedView = localStorage.getItem('op-dash-view');
  if (savedView === 'grid') {
    document.querySelector('[data-view-btn="grid"]')?.click();
  }

  // ── Move-to-folder popover ──────────────────────────────────────────
  let pop = null;
  function closePop() {
    if (pop) { pop.remove(); pop = null; }
  }
  function openMovePopover(anchor, dashId) {
    closePop();
    const item = ITEMS.find(i => i.id === dashId);
    const cur = item && item.folder != null ? String(item.folder) : '';
    const opts = [['', t('— No folder —')], ...FOLDERS.map(f => [String(f.id), f.name])];
    pop = document.createElement('div');
    pop.className = 'move-pop';
    pop.innerHTML = '<div class="move-pop-title mono">' + t('Move to') + '</div>' + opts.map(([v, label]) =>
      '<button class="move-opt' + (v === cur ? ' current' : '') + '" data-v="' + v + '">' +
      '<svg class="ico ico-12"><use href="#ic-folder"/></svg>' + esc(label) +
      (v === cur ? '<svg class="ico ico-12 check"><use href="#ic-check"/></svg>' : '') +
      '</button>').join('');
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    pop.style.top = Math.min(r.bottom + 4 + window.scrollY, window.scrollY + window.innerHeight - pop.offsetHeight - 8) + 'px';
    pop.style.left = Math.max(8, Math.min(r.left + window.scrollX, window.scrollX + window.innerWidth - pop.offsetWidth - 8)) + 'px';
    pop.addEventListener('click', e => {
      const b = e.target.closest('.move-opt');
      if (!b) return;
      const v = b.dataset.v;
      moveDash(dashId, v === '' ? null : parseInt(v, 10));
      closePop();
    });
  }
  document.addEventListener('click', e => {
    if (pop && !e.target.closest('.move-pop') && !e.target.closest('.btn-move') && !e.target.closest('.card-move')) closePop();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePop(); });

  async function moveDash(id, folderId) {
    const res = await fetch('/api/dashboards/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_id: folderId }),
    });
    if (!res.ok) { alert(t('Move failed')); return; }
    const fid = folderId == null ? '' : String(folderId);
    const item = ITEMS.find(i => i.id === id);
    if (item) item.folder = folderId;

    const row = document.querySelector('#dash-tbody tr[data-id="' + id + '"]');
    if (row && !row.classList.contains('fav-row')) {
      row.dataset.folder = fid;
      const header = document.querySelector('#dash-tbody .folder-row[data-folder-id="' + fid + '"]');
      header ? header.after(row) : document.getElementById('dash-tbody').prepend(row);
    }
    const card = document.querySelector('#dash-grid .dash-card[data-id="' + id + '"]');
    if (card && !(item && item.fav != null)) {
      card.dataset.folder = fid;
      const sec = document.querySelector('#dash-grid .folder-sec[data-folder-id="' + fid + '"]');
      sec ? sec.after(card) : document.getElementById('dash-grid').prepend(card);
    }
    apply();
  }

  // ── Folder CRUD + collapse + move (table, delegated) ────────────────
  document.getElementById('btn-new-folder')?.addEventListener('click', async () => {
    const name = prompt(t('Folder name:'));
    if (!name || !name.trim()) return;
    const res = await fetch('/api/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (res.ok) location.reload();
    else alert((await res.json().catch(() => ({}))).detail || t('Failed to create folder'));
  });

  // Shared by the table rows and the grid section headers.
  async function folderAction(e) {
    const ren = e.target.closest('.folder-rename');
    if (ren) {
      e.stopPropagation();
      const name = prompt(t('Rename folder:'), ren.dataset.name);
      if (!name || !name.trim() || name.trim() === ren.dataset.name) return true;
      const res = await fetch('/api/folders/' + ren.dataset.fid, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() }),
      });
      if (res.ok) location.reload();
      else alert((await res.json().catch(() => ({}))).detail || t('Rename failed'));
      return true;
    }
    const del = e.target.closest('.folder-delete');
    if (del) {
      e.stopPropagation();
      if (!confirm(t('Delete folder "{name}"? Its dashboards move to Unsorted.', {name: del.dataset.name}))) return true;
      const res = await fetch('/api/folders/' + del.dataset.fid, { method: 'DELETE' });
      if (res.ok) location.reload();
      else alert(t('Delete failed'));
      return true;
    }
    return false;
  }

  document.getElementById('dash-tbody')?.addEventListener('click', async e => {
    const mv = e.target.closest('.btn-move');
    if (mv) {
      e.preventDefault();
      openMovePopover(mv, mv.dataset.id);
      return;
    }
    if (await folderAction(e)) return;
    const head = e.target.closest('.folder-row');
    if (head) toggleCollapse(head.dataset.folderId || '');
  });

  // ── Grid preview iframes (scale, lazy-load through a concurrency gate) ──
  // At most MAX_INFLIGHT previews download at once: first tiles paint sooner
  // and a burst of heavy renders can't saturate the link or the server.
  const MAX_INFLIGHT = 3;
  let inflight = 0;
  const previewQueue = [];
  function pumpPreviews() {
    while (inflight < MAX_INFLIGHT && previewQueue.length) {
      const f = previewQueue.shift();
      if (!f.dataset.src || f.src) continue;
      inflight++;
      let released = false;
      const release = () => {
        if (released) return;
        released = true;
        inflight--;
        pumpPreviews();
      };
      f.addEventListener('load', () => { f.style.opacity = '1'; release(); }, { once: true });
      f.addEventListener('error', release, { once: true });
      setTimeout(release, 20000); // watchdog: a wedged iframe never blocks the gate
      f.src = f.dataset.src;
    }
  }
  function initGridPreviews() {
    const previews = document.querySelectorAll('#dash-grid .preview');
    previews.forEach(p => {
      const f = p.querySelector('.preview-frame');
      if (!f) return;
      const w = p.offsetWidth;
      const scale = w / 1280;
      f.style.transform = `scale(${scale})`;
    });
    const obs = new IntersectionObserver(entries => {
      entries.forEach(en => {
        if (!en.isIntersecting) return;
        const f = en.target.querySelector('.preview-frame');
        if (f && f.dataset.src && !f.src) previewQueue.push(f);
        obs.unobserve(en.target);
      });
      pumpPreviews();
    }, { rootMargin: '200px' });
    previews.forEach(p => obs.observe(p));
  }

  // Initial pass — respects persisted collapse state on load.
  syncFavButtons();
  initTableSections();
  apply();
})();
