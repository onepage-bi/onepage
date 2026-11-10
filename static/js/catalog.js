/* Catalog — tiles, filters, table list and the table detail drawer.
 *
 * Same idiom as static/js/data-quality.js: the page's only input is the
 * catalogue API (`GET /api/catalog/tables`, identical shape to what is baked
 * into #cat-root at render time); all filtering happens client-side against
 * ONE in-memory snapshot, and every mutation (confirm/reject/reset, rename,
 * owner) ends the same way — refetch that snapshot whole and re-render, so
 * the page can never drift from what a reload would show.
 */
(function () {
  'use strict';

  const root = document.getElementById('cat-root');
  if (!root) return;

  function parseJson(raw, fallback) {
    try { return JSON.parse(raw || ''); } catch (e) { return fallback; }
  }

  let DATA = {
    tables: parseJson(root.dataset.tables, []),
    stats: parseJson(root.dataset.stats, {}),
  };

  const CSRF_SAFE = { 'Content-Type': 'application/json' };
  const isAdmin = !!(window.OP_USER && window.OP_USER.role === 'administrator');

  // ── tiny DOM helper, same idiom as data-quality.js ──────────────────
  function el(tag, attrs, kids) {
    const isNs = !!(attrs && attrs.ns);
    const n = isNs
      ? document.createElementNS(attrs.ns, tag) : document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === 'ns') continue;
      if (k === 'text') { n.textContent = attrs[k]; continue; }
      if (isNs) { n.setAttribute(k, attrs[k]); continue; }
      if (k === 'class') n.className = attrs[k];
      else if (k in n && k !== 'for') n[k] = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(c => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }

  const STATUS_LABELS = { inferred: t('unconfirmed'), confirmed: t('confirmed'), rejected: t('rejected') };

  function fmtAge(iso) {
    if (!iso) return '—';
    const then = new Date(iso).getTime();
    if (isNaN(then)) return '—';
    const days = Math.floor((Date.now() - then) / 86400000);
    if (days <= 0) return t('today');
    if (days === 1) return t('1 day ago');
    return t('{p1} days ago', {p1: days});
  }

  // ── state ─────────────────────────────────────────────────────────
  const state = { q: '', statuses: new Set(), source: '', openTable: null };

  // ── elements ──────────────────────────────────────────────────────
  const tileTables = document.getElementById('cat-tile-tables');
  const tileConfirmed = document.getElementById('cat-tile-confirmed');
  const tileInferred = document.getElementById('cat-tile-inferred');
  const tileNontables = document.getElementById('cat-tile-nontables');
  const searchIn = document.getElementById('cat-search');
  const statusChips = Array.from(document.querySelectorAll('[data-cat-status]'));
  const sourceSel = document.getElementById('cat-source');
  const tbody = document.getElementById('cat-tbody');
  const emptyEl = document.getElementById('cat-empty');
  const rebuildBtn = document.getElementById('cat-rebuild');

  const drawer = document.getElementById('cat-drawer');
  const scrim = document.getElementById('cat-scrim');
  const drawerTitle = drawer && drawer.querySelector('[data-drawer-title]');
  const drawerBody = drawer && drawer.querySelector('[data-drawer-body]');

  if (rebuildBtn && !isAdmin) rebuildBtn.hidden = true;

  // ── refetch ───────────────────────────────────────────────────────
  function refetch() {
    return fetch('/api/catalog/tables')
      .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(json => {
        DATA = json;
        renderAll();
        if (state.openTable) {
          const tv = DATA.tables.find(x => x.table_id === state.openTable);
          if (tv) openDrawer(tv.table_id); else closeDrawer();
        }
      });
  }

  // ── tiles ─────────────────────────────────────────────────────────
  function renderTiles() {
    const s = DATA.stats || {};
    if (tileTables) tileTables.textContent = String(s.tables || 0);
    if (tileConfirmed) tileConfirmed.textContent = String(s.confirmed || 0);
    if (tileInferred) tileInferred.textContent = String(s.inferred || 0);
    if (tileNontables) tileNontables.textContent = String(s.nontables || 0);
  }

  // ── source select ─────────────────────────────────────────────────
  function renderSourceSelect() {
    if (!sourceSel) return;
    const keep = sourceSel.value;
    const bySrc = new Map();
    (DATA.tables || []).forEach(tv => {
      if (tv.source && tv.source.id != null) bySrc.set(String(tv.source.id), tv.source.key_name || String(tv.source.id));
    });
    sourceSel.textContent = '';
    sourceSel.appendChild(el('option', { value: '', text: t('All sources') }));
    Array.from(bySrc.entries()).sort((a, b) => a[1].localeCompare(b[1])).forEach(([id, name]) => {
      sourceSel.appendChild(el('option', { value: id, text: name }));
    });
    if (Array.from(sourceSel.options).some(o => o.value === keep)) sourceSel.value = keep;
  }

  // ── filtering ─────────────────────────────────────────────────────
  function matches(tv) {
    if (state.statuses.size && !state.statuses.has(tv.status)) return false;
    if (state.source && String(tv.source && tv.source.id) !== state.source) return false;
    if (state.q) {
      const q = state.q.toLowerCase();
      const hay = [tv.name, tv.display_name, tv.source && tv.source.key_name]
        .filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function filtered() {
    return (DATA.tables || []).filter(matches);
  }

  // ── table ─────────────────────────────────────────────────────────
  function confBar(v) {
    const pct = v == null ? 0 : Math.round(v * 100);
    const wrap = el('div', { class: 'cat-conf-wrap' });
    const bar = el('div', { class: 'cat-conf-bar' });
    bar.appendChild(el('div', { class: 'cat-conf-fill', style: 'width:' + pct + '%' }));
    wrap.appendChild(bar);
    wrap.appendChild(el('span', { class: 'cat-conf-num mono', text: v == null ? '—' : pct + '%' }));
    return wrap;
  }

  function renderTable() {
    const rows = filtered();
    tbody.textContent = '';
    rows.forEach(tv => {
      const tr = el('tr', {
        class: 'cat-row', 'data-cat-table': tv.table_id, tabindex: '0', role: 'button',
        'aria-haspopup': 'dialog',
      });
      const nameCell = el('td', {});
      nameCell.appendChild(el('div', { class: 'cat-name mono', text: tv.display_name || tv.name }));
      const sub = el('div', { class: 'cat-name-sub' }, [
        el('span', { class: 'cat-src-sub', text: (tv.source && tv.source.key_name) || '—' }),
      ]);
      if (tv.stale) sub.appendChild(el('span', { class: 'cat-stale-badge', text: t('outdated') }));
      nameCell.appendChild(sub);
      tr.appendChild(nameCell);
      tr.appendChild(el('td', {}, [el('span', {
        class: 'cat-badge cat-badge-' + tv.status, text: STATUS_LABELS[tv.status] || tv.status,
      })]));
      tr.appendChild(el('td', {}, [confBar(tv.confidence)]));
      tr.appendChild(el('td', { class: 'mono numeric', text: tv.n_files != null ? String(tv.n_files) : '—' }));
      tr.appendChild(el('td', { class: 'mono numeric', text: String(tv.partitions || 0) }));
      const consCell = el('td', {});
      const consRow = el('div', { class: 'cat-chiprow' });
      (tv.consumers || []).forEach(c => consRow.appendChild(el('span', { class: 'cat-chip', text: c })));
      if (!(tv.consumers || []).length) consRow.appendChild(el('span', { class: 'cat-note', text: '—' }));
      consCell.appendChild(consRow);
      tr.appendChild(consCell);
      tr.appendChild(el('td', { class: 'cat-name-sub', text: (tv.owner && tv.owner.name) || '—' }));
      tr.addEventListener('click', () => openDrawer(tv.table_id));
      tr.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawer(tv.table_id); }
      });
      tbody.appendChild(tr);
    });
    if (emptyEl) emptyEl.hidden = rows.length !== 0;
  }

  function renderAll() {
    renderTiles();
    renderSourceSelect();
    renderTable();
  }

  // ── URL sync ─────────────────────────────────────────────────────
  function pushUrl() {
    const p = new URLSearchParams(location.search);
    if (state.source) p.set('source_id', state.source); else p.delete('source_id');
    const qs = p.toString();
    const url = location.pathname + (qs ? '?' + qs : '');
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', url);
  }

  function pushUrlTable(tableId) {
    const p = new URLSearchParams(location.search);
    if (tableId) p.set('table', tableId); else p.delete('table');
    const qs = p.toString();
    const url = location.pathname + (qs ? '?' + qs : '');
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', url);
  }

  // ── filter wiring ────────────────────────────────────────────────
  if (searchIn) searchIn.addEventListener('input', () => { state.q = searchIn.value.trim(); renderTable(); });
  statusChips.forEach(c => c.addEventListener('click', () => {
    const v = c.dataset.catStatus;
    if (state.statuses.has(v)) { state.statuses.delete(v); c.classList.remove('active'); }
    else { state.statuses.add(v); c.classList.add('active'); }
    renderTable();
  }));
  if (sourceSel) sourceSel.addEventListener('change', () => {
    state.source = sourceSel.value; renderTable(); pushUrl();
  });

  if (rebuildBtn) rebuildBtn.addEventListener('click', async () => {
    rebuildBtn.disabled = true;
    const original = rebuildBtn.textContent;
    rebuildBtn.textContent = t('Calculating…');
    try {
      const res = await fetch('/api/catalog/rebuild', { method: 'POST', headers: CSRF_SAFE });
      if (!res.ok) throw new Error(String(res.status));
      await refetch();
    } catch (e) {
      // best-effort — the periodic job will catch up regardless
    }
    rebuildBtn.disabled = false;
    rebuildBtn.textContent = original;
  });

  // ── drawer sections ──────────────────────────────────────────────
  function statusRow(box, setStatus, table) {
    box.appendChild(el('h3', { text: t('Status') }));
    const row = el('div', { class: 'cat-actions-row' });
    box.appendChild(row);

    function post(path, action) {
      return async () => {
        setStatus(t('Saving…'));
        let res;
        try {
          res = await fetch('/api/catalog/tables/' + table.table_id + '/' + path, {
            method: 'POST', headers: CSRF_SAFE,
            body: JSON.stringify({ version: table.version }),
          });
        } catch (e) { setStatus(t('Could not reach the server.'), 'err'); return; }
        if (res.status === 403) { setStatus(t('No permission for this action.'), 'err'); return; }
        if (res.status === 409) { setStatus(t('Changed by someone else – refreshing…'), 'err'); refetch(); return; }
        if (!res.ok) { setStatus(t('Error ({p1}).', {p1: res.status}), 'err'); return; }
        setStatus(action + '.', 'ok');
        refetch();
      };
    }
    const confirmBtn = el('button', { class: 'btn xs primary', type: 'button', text: t('Confirm') });
    confirmBtn.addEventListener('click', post('confirm', t('Confirmed')));
    const rejectBtn = el('button', { class: 'btn xs', type: 'button', text: t('Reject') });
    rejectBtn.addEventListener('click', post('reject', t('Rejected')));
    row.appendChild(confirmBtn);
    row.appendChild(rejectBtn);
    if (table.status !== 'inferred') {
      const resetBtn = el('button', { class: 'btn xs ghost', type: 'button', text: t('Reset') });
      resetBtn.addEventListener('click', post('reset', t('Reset {ctx:2}')));
      row.appendChild(resetBtn);
    }
  }

  function renameForm(box, setStatus, table) {
    box.appendChild(el('h3', { text: t('Rename') }));
    const nameIn = el('input', { class: 'input sm', type: 'text', value: table.display_name || table.name });
    const saveBtn = el('button', { class: 'btn xs', type: 'button', text: t('Save') });
    box.appendChild(el('div', { class: 'cat-inline-form' }, [nameIn, saveBtn]));
    saveBtn.addEventListener('click', async () => {
      const name = nameIn.value.trim();
      if (!name) { setStatus(t('Name cannot be empty.'), 'err'); return; }
      setStatus(t('Saving…'));
      let res;
      try {
        res = await fetch('/api/catalog/tables/' + table.table_id, {
          method: 'PATCH', headers: CSRF_SAFE,
          body: JSON.stringify({ display_name: name, version: table.version }),
        });
      } catch (e) { setStatus(t('Could not reach the server.'), 'err'); return; }
      if (res.status === 403) { setStatus(t('No permission for this action.'), 'err'); return; }
      if (res.status === 409) { setStatus(t('Changed by someone else – refreshing…'), 'err'); refetch(); return; }
      if (!res.ok) { setStatus(t('Error ({p1}).', {p1: res.status}), 'err'); return; }
      setStatus(t('Renamed.'), 'ok');
      refetch();
    });
  }

  function ownerForm(box, setStatus, table) {
    box.appendChild(el('h3', { text: t('Assign owner') }));
    const idIn = el('input', {
      class: 'input sm', type: 'number', min: '1',
      placeholder: t('User ID'),
      value: table.owner && table.owner.id != null ? table.owner.id : '',
    });
    const saveBtn = el('button', { class: 'btn xs', type: 'button', text: t('Assign') });
    const clearBtn = el('button', { class: 'btn xs ghost', type: 'button', text: t('Unset') });
    box.appendChild(el('div', { class: 'cat-inline-form' }, [idIn, saveBtn, clearBtn]));

    function save(ownerId) {
      return async () => {
        setStatus(t('Saving…'));
        let res;
        try {
          res = await fetch('/api/catalog/tables/' + table.table_id, {
            method: 'PATCH', headers: CSRF_SAFE,
            body: JSON.stringify({ owner_id: ownerId, version: table.version }),
          });
        } catch (e) { setStatus(t('Could not reach the server.'), 'err'); return; }
        if (res.status === 403) { setStatus(t('No permission for this action.'), 'err'); return; }
        if (res.status === 409) { setStatus(t('Changed by someone else – refreshing…'), 'err'); refetch(); return; }
        if (res.status === 422) { setStatus(t('No such user.'), 'err'); return; }
        if (!res.ok) { setStatus(t('Error ({p1}).', {p1: res.status}), 'err'); return; }
        setStatus(t('Saved.'), 'ok');
        refetch();
      };
    }
    saveBtn.addEventListener('click', () => {
      const v = idIn.value.trim();
      if (!v) { setStatus(t('Specify user ID.'), 'err'); return; }
      save(Number(v))();
    });
    clearBtn.addEventListener('click', save(null));
  }

  function renderColumns(box, table) {
    if (!(table.columns || []).length) return;
    box.appendChild(el('h3', { text: t('Columns') }));
    const tbl = el('table', { class: 'cat-mini-table' });
    const thead = el('thead', {}, [el('tr', {}, [
      el('th', { text: t('Column') }), el('th', { text: t('Type') }),
      el('th', { class: 'numeric', text: t('% files') }),
    ])]);
    tbl.appendChild(thead);
    const tb = el('tbody');
    table.columns.forEach(c => {
      tb.appendChild(el('tr', {}, [
        el('td', { class: 'mono', text: c.display || c.name }),
        el('td', { class: 'mono', text: c.type || '—' }),
        el('td', { class: 'numeric mono', text: c.present_ratio != null ? Math.round(c.present_ratio * 100) + '%' : '—' }),
      ]));
    });
    tbl.appendChild(tb);
    box.appendChild(tbl);
  }

  function fmtBytes(n) {
    if (n == null) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function renderPartitions(box, table) {
    if (!(table.partitions || []).length) return;
    box.appendChild(el('h3', { text: t('Partitions') }));
    const list = el('div', { class: 'cat-timeline' });
    table.partitions.forEach(p => {
      const row = el('div', { class: 'cat-timeline-row' });
      row.appendChild(el('span', { class: 'cat-timeline-key', text: p.key || '—' }));
      row.appendChild(el('span', { class: 'cat-timeline-file mono', text: p.file + (p.sheet ? ' › ' + p.sheet : ''), title: p.file }));
      row.appendChild(el('span', {
        class: 'cat-timeline-meta mono',
        text: (p.rows != null ? t('{p1} pages', {p1: p.rows}) : '—') + ' · ' + fmtBytes(p.size),
      }));
      list.appendChild(row);
    });
    box.appendChild(list);
  }

  function renderConsumers(box, table, setStatus) {
    if (!(table.consumers || []).length) return;
    box.appendChild(el('h3', { text: t('Consumers') }));
    const row = el('div', { class: 'cat-chiprow' });
    table.consumers.forEach(c => row.appendChild(el('span', {
      class: 'cat-chip', text: c.generator_name + (c.legacy ? ' · legacy' : ''),
    })));
    box.appendChild(row);

    const legacy = table.consumers.filter(c => c.legacy);
    legacy.forEach(c => renderSuggestInputs(box, c, setStatus));
  }

  function renderSuggestInputs(box, consumer, setStatus) {
    const wrap = el('div', { class: 'cat-suggest-box' });
    const toggle = el('button', {
      class: 'cat-why-btn', type: 'button',
      text: t('Suggest inputs for «{p1}»', {p1: consumer.generator_name}),
    });
    const body = el('div', { hidden: true });
    wrap.appendChild(toggle);
    wrap.appendChild(body);
    box.appendChild(wrap);

    let loaded = false;
    toggle.addEventListener('click', async () => {
      body.hidden = !body.hidden;
      if (body.hidden || loaded) return;
      body.textContent = '';
      body.appendChild(el('div', { class: 'cat-note', text: t('Loading…') }));
      let patch;
      try {
        const r = await fetch('/api/catalog/suggest-inputs/' + consumer.generator_id);
        if (!r.ok) throw new Error(String(r.status));
        patch = await r.json();
      } catch (e) {
        body.textContent = '';
        body.appendChild(el('div', { class: 'cat-note', text: t('Failed to load suggestion.') }));
        return;
      }
      loaded = true;
      body.textContent = '';
      const jsonBox = el('div', { class: 'cat-suggest-json', text: patch.inputs_json || '[]' });
      const copyBtn = el('button', { class: 'btn xs', type: 'button', text: t('Copy JSON') });
      copyBtn.addEventListener('click', () => {
        const text = patch.inputs_json || '[]';
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(
            () => { copyBtn.textContent = t('Copied'); setTimeout(() => { copyBtn.textContent = t('Copy JSON'); }, 1500); },
            () => { copyBtn.textContent = t('Failed'); }
          );
        }
      });
      body.appendChild(jsonBox);
      body.appendChild(copyBtn);
      body.appendChild(el('div', {
        class: 'cat-warn-box',
        text: t('After declaring inputs, the engine will NOT pass files from the list below – \'unmatched\'.'),
      }));
      const matched = (patch.matched || []);
      const unmatched = (patch.unmatched || []);
      body.appendChild(el('div', { class: 'cat-note', text: t('Will match ({p1}):', {p1: matched.length}) }));
      body.appendChild(el('div', { class: 'cat-file-list mono', text: matched.join(', ') || '—' }));
      body.appendChild(el('div', { class: 'cat-note', text: t('Will NOT match ({p1}):', {p1: unmatched.length}) }));
      body.appendChild(el('div', { class: 'cat-file-list mono', text: unmatched.join(', ') || '—' }));
    });
  }

  function renderEvidence(box, table) {
    if (!(table.evidence || []).length) return;
    const btn = el('button', { class: 'cat-why-btn', type: 'button', text: t('Why? ({p1})', {p1: table.evidence.length}) });
    const list = el('div', { class: 'cat-evidence' });
    table.evidence.forEach(e => {
      list.appendChild(el('div', { class: 'cat-nontable-row' }, [
        el('span', { class: 'mono', text: e.file }),
        el('span', { class: 'mono', text: e.sheet || '' }),
        el('span', { class: 'mono', text: 'sim=' + (e.sim != null ? e.sim : '—') }),
      ]));
    });
    btn.addEventListener('click', () => list.classList.toggle('open'));
    box.appendChild(btn);
    box.appendChild(list);
  }

  function renderNontables(box, table) {
    if (!(table.nontables || []).length) return;
    box.appendChild(el('h3', { text: t('Header not recognized (same source)') }));
    const list = el('div', {});
    table.nontables.slice(0, 20).forEach(n => {
      list.appendChild(el('div', { class: 'cat-nontable-row' }, [
        el('span', { class: 'mono', text: n.file }),
        el('span', { class: 'mono', text: n.sheet || '' }),
        el('span', { class: 'cat-chip', text: n.kind }),
        el('span', { class: 'cat-note', text: n.reason || '' }),
      ]));
    });
    box.appendChild(list);
  }

  // ── drawer open/close ────────────────────────────────────────────
  let lastFocus = null;
  function openDrawer(tableId) {
    if (state.openTable !== tableId) lastFocus = document.activeElement;
    state.openTable = tableId;
    if (!drawer) return;
    drawerTitle.textContent = tableId;
    drawerBody.textContent = '';
    drawerBody.appendChild(el('div', { class: 'cat-note', text: t('Loading…') }));
    drawer.hidden = false;
    if (scrim) scrim.hidden = false;
    pushUrlTable(tableId);

    fetch('/api/catalog/tables/' + encodeURIComponent(tableId))
      .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(renderDrawer)
      .catch(() => {
        drawerBody.textContent = '';
        drawerBody.appendChild(el('div', { class: 'cat-note', text: t('Failed to load table.') }));
      });
  }

  function renderDrawer(table) {
    if (state.openTable !== table.table_id) return;
    drawerTitle.textContent = table.display_name || table.name;
    drawerBody.textContent = '';

    const head = el('div', { class: 'cat-drawer-sect' });
    head.appendChild(el('span', { class: 'cat-badge cat-badge-' + table.status, text: STATUS_LABELS[table.status] || table.status }));
    head.appendChild(el('div', { class: 'cat-dl' }, [
      el('div', {}, [el('dt', { text: t('Source') }), el('dd', { class: 'mono', text: (table.source && table.source.key_name) || '—' })]),
      el('div', {}, [el('dt', { text: t('Confidence') }), el('dd', { class: 'mono', text: table.confidence != null ? Math.round(table.confidence * 100) + '%' : '—' })]),
      el('div', {}, [el('dt', { text: t('Files') }), el('dd', { class: 'mono', text: table.n_files != null ? String(table.n_files) : '—' })]),
      el('div', {}, [el('dt', { text: t('Owner') }), el('dd', { class: 'mono', text: (table.owner && table.owner.name) || '—' })]),
      el('div', {}, [el('dt', { text: t('First seen') }), el('dd', { class: 'mono', text: fmtAge(table.first_seen_at) })]),
      el('div', {}, [el('dt', { text: t('Last time') }), el('dd', { class: 'mono', text: fmtAge(table.last_seen_at) })]),
    ]));
    if (table.freshness) {
      head.appendChild(el('p', { class: 'cat-note', text: t('Data is fresh as of {p1} days ago.', {p1: table.freshness.days_since}) }));
    }
    renderEvidence(head, table);
    drawerBody.appendChild(head);

    const colsSect = el('div', { class: 'cat-drawer-sect' });
    renderColumns(colsSect, table);
    if (colsSect.children.length) drawerBody.appendChild(colsSect);

    const partsSect = el('div', { class: 'cat-drawer-sect' });
    renderPartitions(partsSect, table);
    if (partsSect.children.length) drawerBody.appendChild(partsSect);

    const status = el('span', { class: 'cat-status mono' });
    function setStatus(msg, kind) {
      status.textContent = msg || '';
      status.style.color = kind === 'err' ? 'var(--danger)' : kind === 'ok' ? 'var(--success)' : 'var(--ink-dim)';
    }

    const consSect = el('div', { class: 'cat-drawer-sect' });
    renderConsumers(consSect, table, setStatus);
    if (consSect.children.length) drawerBody.appendChild(consSect);

    const actionsSect = el('div', { class: 'cat-drawer-sect' });
    statusRow(actionsSect, setStatus, table);
    renameForm(actionsSect, setStatus, table);
    ownerForm(actionsSect, setStatus, table);
    actionsSect.appendChild(status);
    drawerBody.appendChild(actionsSect);

    const ntSect = el('div', { class: 'cat-drawer-sect' });
    renderNontables(ntSect, table);
    if (ntSect.children.length) drawerBody.appendChild(ntSect);

    const closeBtnEl = drawer.querySelector('[data-drawer-close]');
    if (closeBtnEl && closeBtnEl.focus) closeBtnEl.focus();
  }

  function closeDrawer() {
    if (!drawer) return;
    drawer.hidden = true;
    if (scrim) scrim.hidden = true;
    state.openTable = null;
    pushUrlTable(null);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
  }

  if (scrim) scrim.addEventListener('click', closeDrawer);
  const closeBtn = drawer && drawer.querySelector('[data-drawer-close]');
  if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && drawer && !drawer.hidden) closeDrawer();
  });

  // ── init from URL params ────────────────────────────────────────
  function initFromUrl() {
    const p = new URLSearchParams(location.search);
    const sourceId = p.get('source_id');
    const tableId = p.get('table');
    if (sourceId && sourceSel) { sourceSel.value = sourceId; state.source = sourceId; }
    renderTable();
    if (tableId) openDrawer(tableId);
  }

  renderAll();
  initFromUrl();
})();
