/* Data Quality — filters, findings table and the finding drawer.
 *
 * The page's only input is the evaluator's own output (`GET /api/data-quality`,
 * identical shape to the JSON already baked into #dq-root by the server): all
 * filtering happens client-side against ONE in-memory snapshot, and every
 * mutation (ack, accept, alias, thresholds) ends the same way — refetch that
 * snapshot whole and re-render, so the page can never drift from what the
 * evaluator would say if you reloaded it.
 */
(function () {
  'use strict';

  const root = document.getElementById('dq-root');
  if (!root) return;

  function parseJson(raw, fallback) {
    try { return JSON.parse(raw || ''); } catch (e) { return fallback; }
  }

  let DATA = {
    sources: parseJson(root.dataset.sources, []),
    findings: parseJson(root.dataset.findings, []),
    pending_values: parseJson(root.dataset.pending, []),
    history: parseJson(root.dataset.history, {}),
    stats: parseJson(root.dataset.stats, {}),
  };

  const CSRF_SAFE = { 'Content-Type': 'application/json' };

  // ── kind taxonomy ─────────────────────────────────────────────────
  const KIND_GROUPS = {
    schema: ['schema_missing_pattern', 'schema_missing_sheet', 'schema_missing_column',
             'schema_extra_column', 'schema_unexpected_file', 'schema_unreadable'],
    freshness: ['file_stale', 'file_count_drop', 'required_file_gone', 'rows_drop', 'rows_zero',
                'rerun_needed'],
    checks: ['check_fail'],
    drift: ['new_values', 'unmapped_values'],
    other: ['header_shift', 'dead_declaration'],
  };
  const GROUP_OF_KIND = {};
  Object.keys(KIND_GROUPS).forEach(g => KIND_GROUPS[g].forEach(k => { GROUP_OF_KIND[k] = g; }));

  const KIND_LABELS = {
    schema_missing_pattern: t('missing pattern'), schema_missing_sheet: t('missing sheet'),
    schema_missing_column: t('missing column'), schema_extra_column: t('extra column'),
    schema_unexpected_file: t('unexpected file'), schema_unreadable: t('header not found'),
    file_stale: t('stale'),
    file_count_drop: t('file count drop'), required_file_gone: t('required file gone'),
    rows_drop: t('rows drop'), rows_zero: t('rows zero'), check_fail: t('check fail'),
    new_values: t('new values'), header_shift: t('header shift'), dead_declaration: t('dead declaration'),
    // ADR-16 reference tables.
    unmapped_values: t('unmapped values'), rerun_needed: t('rerun needed'),
  };

  // ── tiny DOM helper, same idiom as data-sources-edit.js ─────────────
  // SVG elements' IDL attributes (className, viewBox, width, points, ...)
  // are read-only getters — `n[k] = v` throws under 'use strict'. Namespaced
  // elements therefore always go through setAttribute, exactly like
  // lineage.js does for the same reason.
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
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function fmtAge(iso) {
    if (!iso) return '—';
    const then = new Date(iso).getTime();
    if (isNaN(then)) return '—';
    const days = Math.floor((Date.now() - then) / 86400000);
    if (days <= 0) return t('today');
    if (days === 1) return t('1d ago');
    return t('{n}d ago', {n: days});
  }

  // ── state ─────────────────────────────────────────────────────────
  const state = {
    q: '', severities: new Set(), groups: new Set(), source: '', generator: '',
    showAcked: false, openFinding: null,
  };

  // ── elements ──────────────────────────────────────────────────────
  const tileOk = document.getElementById('dq-tile-ok');
  const tileWarn = document.getElementById('dq-tile-warn');
  const tileErr = document.getElementById('dq-tile-error');
  const tileOpen = document.getElementById('dq-tile-open');
  const tilePending = document.getElementById('dq-tile-pending');
  const tileStale = document.getElementById('dq-tile-stale');
  const searchIn = document.getElementById('dq-search');
  const sevChips = Array.from(document.querySelectorAll('[data-dq-sev]'));
  const groupChips = Array.from(document.querySelectorAll('[data-dq-group]'));
  const sourceSel = document.getElementById('dq-source');
  const genSel = document.getElementById('dq-generator');
  const ackedToggle = document.getElementById('dq-show-acked');
  const tbody = document.getElementById('dq-tbody');
  const emptyEl = document.getElementById('dq-empty');
  const thresholdsPanel = document.getElementById('dq-thresholds');

  const drawer = document.getElementById('dq-drawer');
  const scrim = document.getElementById('dq-scrim');
  const drawerTitle = drawer && drawer.querySelector('[data-drawer-title]');
  const drawerBody = drawer && drawer.querySelector('[data-drawer-body]');

  // ── refetch ───────────────────────────────────────────────────────
  function refetch() {
    return fetch('/api/data-quality')
      .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(json => {
        DATA = json;
        renderAll();
        if (state.openFinding) {
          const f = DATA.findings.find(x => x.id === state.openFinding);
          if (f) openDrawer(f.id); else closeDrawer();
        }
      });
  }

  // ── tiles ─────────────────────────────────────────────────────────
  function renderTiles() {
    const s = DATA.stats || {};
    if (tileOk) tileOk.textContent = String(s.sources_ok || 0);
    if (tileWarn) tileWarn.textContent = String(s.sources_warn || 0);
    if (tileErr) tileErr.textContent = String(s.sources_error || 0);
    if (tileOpen) tileOpen.textContent = String(s.open || 0);
    if (tilePending) tilePending.textContent = String(s.pending_values || 0);
    if (tileStale) tileStale.textContent = String(s.stale || 0);
  }

  // ── filter selects (rebuilt from the current snapshot) ─────────────
  function renderSelects() {
    if (sourceSel) {
      const keep = sourceSel.value;
      sourceSel.textContent = '';
      sourceSel.appendChild(el('option', { value: '', text: t('All sources') }));
      (DATA.sources || []).slice().sort((a, b) => a.name.localeCompare(b.name)).forEach(s => {
        sourceSel.appendChild(el('option', { value: String(s.source_id), text: s.name }));
      });
      if (Array.from(sourceSel.options).some(o => o.value === keep)) sourceSel.value = keep;
    }
    if (genSel) {
      const keep = genSel.value;
      const names = Array.from(new Set((DATA.findings || []).map(f => f.generator).filter(Boolean))).sort();
      genSel.textContent = '';
      genSel.appendChild(el('option', { value: '', text: t('All generators') }));
      names.forEach(n => genSel.appendChild(el('option', { value: n, text: n })));
      if (Array.from(genSel.options).some(o => o.value === keep)) genSel.value = keep;
    }
  }

  // ── filtering ─────────────────────────────────────────────────────
  function matches(f) {
    if (state.severities.size && !state.severities.has(f.severity)) return false;
    if (state.groups.size && !state.groups.has(GROUP_OF_KIND[f.kind] || 'other')) return false;
    if (state.source && String(f.source_id) !== state.source) return false;
    if (state.generator && f.generator !== state.generator) return false;
    if (f.acked && !state.showAcked) return false;
    if (state.q) {
      const q = state.q.toLowerCase();
      const hay = [f.message, f.source, f.generator, f.file, f.sheet, f.column]
        .filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function filtered() {
    return (DATA.findings || []).filter(matches);
  }

  // ── table ─────────────────────────────────────────────────────────
  function locator(f) {
    return [f.file, f.sheet, f.column].filter(Boolean).join(' › ') || '—';
  }

  function renderTable() {
    const rows = filtered();
    tbody.textContent = '';
    rows.forEach(f => {
      const isSampled = f.kind === 'check_fail' && f.detail && f.detail.sampled;
      const tr = el('tr', {
        class: 'dq-row dq-sev-' + f.severity + (f.acked ? ' is-acked' : '') + (isSampled ? ' is-sampled' : ''),
        'data-dq-finding': f.id, tabindex: '0', role: 'button',
        'aria-haspopup': 'dialog',
      });
      const kindCell = el('td', {}, [el('span', { class: 'dq-badge dq-kind-' + f.kind, text: KIND_LABELS[f.kind] || f.kind })]);
      if (isSampled) {
        kindCell.appendChild(el('span', { class: 'dq-badge dq-badge-sampled', text: t('Sample') }));
      }
      tr.appendChild(kindCell);
      tr.appendChild(el('td', { class: 'dq-msg', text: f.message }));
      tr.appendChild(el('td', { class: 'mono dq-sub', text: f.source }));
      tr.appendChild(el('td', { class: 'mono dq-sub', text: f.generator || '—' }));
      tr.appendChild(el('td', { class: 'mono dq-sub dq-loc', text: locator(f), title: locator(f) }));
      tr.appendChild(el('td', { class: 'mono numeric', text: f.since_run != null ? '#' + f.since_run : '—' }));
      tr.appendChild(el('td', { class: 'mono numeric', text: fmtAge(f.first_seen_at) }));
      tr.addEventListener('click', () => openDrawer(f.id));
      tr.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawer(f.id); }
      });
      tbody.appendChild(tr);
    });
    renderEmptyState(rows.length);
  }

  // The generic "no findings match this filter" message is right for a
  // combination of chips someone stacked up themselves, but the viewer's
  // "Data Quality" link (`?generator=X`) can point at a generator with zero
  // findings at all — the filter select never even gets an option for it
  // (renderSelects only lists generators that HAVE findings), so the reader
  // lands on a table that just looks broken. Name the generator and give a
  // one-click way back to the unfiltered list.
  function renderEmptyState(rowCount) {
    if (!emptyEl) return;
    emptyEl.hidden = rowCount !== 0;
    if (rowCount !== 0) return;
    emptyEl.textContent = '';
    if (state.generator) {
      emptyEl.appendChild(document.createTextNode(
        t('No findings for generator "{name}". ', {name: state.generator})));
      const clearBtn = el('button', { type: 'button', class: 'dq-empty-clear', text: t('Clear filter') });
      clearBtn.addEventListener('click', () => {
        state.generator = '';
        if (genSel) genSel.value = '';
        renderTable();
        pushUrl();
      });
      emptyEl.appendChild(clearBtn);
    } else {
      emptyEl.textContent = t('No findings match this filter.');
    }
  }

  function renderThresholds() {
    if (!thresholdsPanel) return;
    if (!state.source) { thresholdsPanel.hidden = true; return; }
    const s = (DATA.sources || []).find(x => String(x.source_id) === state.source);
    if (!s) { thresholdsPanel.hidden = true; return; }
    thresholdsPanel.hidden = false;
    thresholdsPanel.textContent = '';
    // stale_after_days is the raw (possibly-null) column: a blank field means
    // "inherit the installation default", shown as a placeholder rather than
    // baked into value= — so leaving it untouched round-trips as null, not
    // as a value that silently pins today's default onto this source.
    const staleIn = el('input', {
      class: 'input sm', type: 'number', min: '1',
      value: s.stale_after_days == null ? '' : s.stale_after_days,
      placeholder: String(s.stale_after_days_default || ''),
    });
    const filesIn = el('input', { class: 'input sm', type: 'number', min: '0', value: s.min_files == null ? '' : s.min_files });
    const dropIn = el('input', { class: 'input sm', type: 'number', min: '1', max: '100',
      value: (s.thresholds && s.thresholds.drop_pct != null) ? s.thresholds.drop_pct : '' });
    const status = el('span', { class: 'dq-status mono' });
    const saveBtn = el('button', { class: 'btn xs primary', type: 'button', text: t('Save thresholds') });
    thresholdsPanel.appendChild(el('div', { class: 'dq-thr-row' }, [
      el('label', { class: 'dq-lbl', text: t('Stale after (days)') }), staleIn,
      el('label', { class: 'dq-lbl', text: t('Min files') }), filesIn,
      el('label', { class: 'dq-lbl', text: t('Drop %') }), dropIn,
      saveBtn, status,
    ]));
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true; status.textContent = t('Saving…');
      const [kind, keyName] = s.key.split(/:(.*)/s);
      const body = {
        kind: kind, key_name: keyName, version: s.version,
        stale_after_days: staleIn.value === '' ? null : Number(staleIn.value),
        min_files: filesIn.value === '' ? null : Number(filesIn.value),
        thresholds: {
          min_rows: (s.thresholds && s.thresholds.min_rows) || {},
          drop_pct: dropIn.value === '' ? null : Number(dropIn.value),
        },
      };
      let res;
      try {
        res = await fetch('/api/data-sources', { method: 'PUT', headers: CSRF_SAFE, body: JSON.stringify(body) });
      } catch (e) {
        saveBtn.disabled = false; status.textContent = t('Could not reach the server.'); return;
      }
      saveBtn.disabled = false;
      if (res.status === 409) { status.textContent = t('changed elsewhere — reload'); refetch(); return; }
      if (!res.ok) { status.textContent = t('Save failed ({status}).', {status: res.status}); return; }
      status.textContent = t('Saved.');
      refetch();
    });
  }

  function renderAll() {
    renderTiles();
    renderSelects();
    renderTable();
    renderThresholds();
  }

  // ── URL sync ─────────────────────────────────────────────────────
  function pushUrl() {
    const p = new URLSearchParams(location.search);
    if (state.source) p.set('source_id', state.source); else p.delete('source_id');
    if (state.generator) p.set('generator', state.generator); else p.delete('generator');
    const qs = p.toString();
    const url = location.pathname + (qs ? '?' + qs : '');
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', url);
  }

  // ── filter wiring ────────────────────────────────────────────────
  if (searchIn) searchIn.addEventListener('input', () => { state.q = searchIn.value.trim(); renderTable(); });
  sevChips.forEach(c => c.addEventListener('click', () => {
    const v = c.dataset.dqSev;
    if (state.severities.has(v)) { state.severities.delete(v); c.classList.remove('active'); }
    else { state.severities.add(v); c.classList.add('active'); }
    renderTable();
  }));
  groupChips.forEach(c => c.addEventListener('click', () => {
    const v = c.dataset.dqGroup;
    if (state.groups.has(v)) { state.groups.delete(v); c.classList.remove('active'); }
    else { state.groups.add(v); c.classList.add('active'); }
    renderTable();
  }));
  if (sourceSel) sourceSel.addEventListener('change', () => {
    state.source = sourceSel.value; renderTable(); renderThresholds(); pushUrl();
  });
  if (genSel) genSel.addEventListener('change', () => {
    state.generator = genSel.value; renderTable(); pushUrl();
  });
  if (ackedToggle) ackedToggle.addEventListener('change', () => {
    state.showAcked = ackedToggle.checked; renderTable();
  });

  // ── sparklines ───────────────────────────────────────────────────
  function sparkline(values) {
    const raw = values || [];
    const present = raw.filter(v => v !== null && v !== undefined);
    const wrap = el('div', { class: 'dq-spark' });
    if (present.length < 2) {
      wrap.appendChild(el('span', { class: 'dq-note', text: present.length ? String(present[0]) : t('no history') }));
      return wrap;
    }
    // history arrives newest-first; draw chronologically (oldest -> newest),
    // keeping every run's true index so a missing run leaves a gap instead
    // of pulling its neighbours together (see _history_series's contract:
    // every list is the same length as `runs`, null where a run had no value).
    const chrono = raw.slice().reverse();
    const w = 120, h = 28, pad = 2;
    const max = Math.max.apply(null, present), min = Math.min.apply(null, present);
    const range = (max - min) || 1;
    const step = chrono.length > 1 ? (w - pad * 2) / (chrono.length - 1) : 0;
    const svg = el('svg', { ns: SVG_NS, class: 'dq-spark-svg', viewBox: '0 0 ' + w + ' ' + h,
      width: String(w), height: String(h) });
    let seg = [];
    function flush() {
      if (seg.length > 1) {
        svg.appendChild(el('polyline', { ns: SVG_NS, class: 'dq-spark-line', points: seg.join(' ') }));
      } else if (seg.length === 1) {
        const [x, y] = seg[0].split(',');
        svg.appendChild(el('circle', { ns: SVG_NS, class: 'dq-spark-dot', cx: x, cy: y, r: '1.5' }));
      }
      seg = [];
    }
    chrono.forEach((v, i) => {
      if (v === null || v === undefined) { flush(); return; }
      const x = pad + i * step;
      const y = h - pad - ((v - min) / range) * (h - pad * 2);
      seg.push(x.toFixed(1) + ',' + y.toFixed(1));
    });
    flush();
    wrap.appendChild(svg);
    return wrap;
  }

  // ── drawer detail renderers, one per kind family ────────────────
  function chip(label, value, cls) {
    return el('span', { class: 'dq-chip' + (cls ? ' ' + cls : ''), text: label + ': ' + value });
  }

  function renderCheckFail(f, box) {
    const row = el('div', { class: 'dq-chiprow' });
    row.appendChild(chip(t('count'), f.detail.count != null ? f.detail.count : '—'));
    row.appendChild(chip(t('failed'), f.detail.failed != null ? f.detail.failed : '—', 'dq-warn'));
    if (f.detail.reason) row.appendChild(chip(t('skipped'), f.detail.reason, 'dq-info'));
    if (f.detail.sampled) row.appendChild(el('span', { class: 'dq-badge dq-badge-sampled', text: t('Sample') }));
    box.appendChild(row);
    if (f.detail.sampled) {
      box.appendChild(el('div', {
        class: 'dq-note',
        text: t('Validated on sample: {p1} rows – not on entire file.', {p1: (f.detail.count != null ? f.detail.count : '—')}),
      }));
    }
    if (f.detail.examples && f.detail.examples.length) {
      const ex = el('div', { class: 'dq-chiprow' });
      f.detail.examples.forEach(v => ex.appendChild(el('code', { class: 'dq-example', text: String(v) })));
      box.appendChild(el('label', { class: 'dq-lbl', text: t('Examples') }));
      box.appendChild(ex);
    }
  }

  function renderNewValues(f, box, setStatus) {
    const pending = (DATA.pending_values || []).find(p =>
      p.source_id === f.source_id && p.generator === f.generator &&
      p.match === f.file && p.sheet === f.sheet && p.column === f.column);
    const values = (pending && pending.new) || f.detail.new || [];
    if (!values.length) {
      box.appendChild(el('div', { class: 'dq-note', text: t('No pending values to accept.') }));
      return;
    }
    const boxes = [];
    const list = el('div', { class: 'dq-checklist' });
    values.forEach(v => {
      const cb = el('input', { type: 'checkbox', checked: true, value: v });
      boxes.push(cb);
      list.appendChild(el('label', { class: 'dq-checkrow' }, [cb, el('span', { class: 'mono', text: v })]));
    });
    box.appendChild(list);
    if (!pending) {
      box.appendChild(el('div', { class: 'dq-note', text: t('No baseline generator on record — cannot accept from here.') }));
      return;
    }
    const btn = el('button', { class: 'btn xs primary', type: 'button', text: t('Accept selected') });
    box.appendChild(btn);
    btn.addEventListener('click', async () => {
      const accept = boxes.filter(b => b.checked).map(b => b.value);
      if (!accept.length) return;
      btn.disabled = true; setStatus(t('Saving…'));
      let res;
      try {
        res = await fetch('/api/data-sources/' + f.source_id + '/baselines/' + pending.generator_id, {
          method: 'POST', headers: CSRF_SAFE,
          body: JSON.stringify({
            match: pending.match, sheet: pending.sheet, column: pending.column,
            accept: accept, version: pending.baseline_version,
          }),
        });
      } catch (e) {
        btn.disabled = false; setStatus(t('Could not reach the server.'), 'err'); return;
      }
      if (res.status === 409) {
        btn.disabled = false; setStatus(t('changed elsewhere — reload'), 'err');
        refetch();
        return;
      }
      if (!res.ok) { btn.disabled = false; setStatus(t('Save failed ({status}).', {status: res.status}), 'err'); return; }
      setStatus(t('Accepted.'), 'ok');
      refetch();
    });
  }

  // ── ADR-16 reference tables ──────────────────────────────────────
  // Every string below is user or author text and goes in through `text:`
  // (textContent), never innerHTML — same rule the XSS pin enforces.

  function renderUnmappedValues(f, box) {
    const d = f.detail || {};
    const values = d.values || [];
    if (d.exists === false) {
      box.appendChild(el('div', {
        class: 'dq-note',
        text: t('Reference table «{p1}» not yet created.', {p1: String(d.table || '')}),
      }));
    }
    if (d.overflow) {
      box.appendChild(el('div', {
        class: 'dq-note',
        text: t('Too many mismatches – list below is incomplete.'),
      }));
    }
    if (values.length) {
      box.appendChild(el('label', { class: 'dq-lbl', text: t('Not in reference table') }));
      const row = el('div', { class: 'dq-chiprow' });
      values.forEach(v => row.appendChild(el('code', { class: 'dq-example', text: String(v) })));
      box.appendChild(row);
      if (d.more) {
        box.appendChild(el('div', { class: 'dq-note', text: t('… and {p1} more', {p1: String(d.more)}) }));
      }
    }
    if (d.responsible && d.responsible.display_name) {
      box.appendChild(el('div', {
        class: 'dq-note', text: t('Owner: {p1} {ctx:2}', {p1: String(d.responsible.display_name)}),
      }));
    } else if (d.exists) {
      box.appendChild(el('div', { class: 'dq-note', text: t('Owner not assigned.') }));
    }
    const links = el('div', { class: 'dq-chiprow' });
    if (d.reference_url) {
      links.appendChild(el('a', { class: 'btn xs', href: d.reference_url,
                                  text: t('Open reference table') }));
    }
    if (d.template_url) {
      // The link carries its keys in the URL, so a long list is truncated to
      // keep the request line legal (quality.MAX_TEMPLATE_URL_BYTES). Say so
      // rather than hand back a workbook quietly shorter than the count above.
      links.appendChild(el('a', {
        class: 'btn xs ghost', href: d.template_url,
        text: d.template_partial
          ? t('Download template (first {p1})', {p1: String(d.template_keys || 0)})
          : t('Download new values template'),
      }));
    }
    if (links.children && links.children.length) box.appendChild(links);
  }

  function renderRerunNeeded(f, box) {
    const d = f.detail || {};
    const used = (d.used_version === null || d.used_version === undefined)
      ? t('without reference table') : 'v' + String(d.used_version);
    box.appendChild(el('div', {
      class: 'dq-note',
      text: t('Last run: {p1} · current version: v{p2}', {p1: used, p2: String(d.current_version || 0)}),
    }));
    if (d.responsible && d.responsible.display_name) {
      box.appendChild(el('div', {
        class: 'dq-note', text: t('Owner: {p1} {ctx:2}', {p1: String(d.responsible.display_name)}),
      }));
    }
    const links = el('div', { class: 'dq-chiprow' });
    if (d.dashboard_id) {
      links.appendChild(el('a', { class: 'btn xs', href: '/d/' + String(d.dashboard_id),
                                  text: t('Open report') }));
    }
    if (d.reference_url) {
      links.appendChild(el('a', { class: 'btn xs ghost', href: d.reference_url,
                                  text: t('Open reference table') }));
    }
    if (links.children && links.children.length) box.appendChild(links);
  }

  function renderSchemaMissingColumn(f, box, setStatus) {
    if (!f.detail.suggest) return;
    const btn = el('button', {
      class: 'btn xs', type: 'button',
      text: t('Treat «{col}» as alias of «{suggest}»', {col: f.column, suggest: f.detail.suggest}),
    });
    box.appendChild(btn);
    btn.addEventListener('click', async () => {
      btn.disabled = true; setStatus(t('Loading current overrides…'));
      let list;
      try {
        const r = await fetch('/api/data-sources/' + f.source_id + '/overrides');
        if (!r.ok) throw new Error(String(r.status));
        list = await r.json();
      } catch (e) {
        btn.disabled = false; setStatus(t('Could not reach the server.'), 'err'); return;
      }
      const entry = (list || []).find(x => x.generator_name === f.generator);
      if (!entry) { btn.disabled = false; setStatus(t('Generator has no observed overrides row.'), 'err'); return; }
      const key = f.file + '/' + f.sheet;
      const aliases = Object.assign({}, entry.column_aliases || {});
      aliases[key] = Object.assign({}, aliases[key] || {}, { [f.column]: f.detail.suggest });
      let res;
      try {
        res = await fetch('/api/data-sources/' + f.source_id + '/overrides/' + entry.generator_id, {
          method: 'PUT', headers: CSRF_SAFE,
          body: JSON.stringify({ exclude_rules: entry.exclude_rules || [], column_aliases: aliases, version: entry.version }),
        });
      } catch (e) {
        btn.disabled = false; setStatus(t('Could not reach the server.'), 'err'); return;
      }
      if (res.status === 409) {
        btn.disabled = false; setStatus(t('changed elsewhere — reload'), 'err');
        refetch();
        return;
      }
      if (!res.ok) { btn.disabled = false; setStatus(t('Save failed ({status}).', {status: res.status}), 'err'); return; }
      setStatus(t('Alias saved.'), 'ok');
      refetch();
    });
  }

  function renderAffected(f, box) {
    const affected = f.detail && f.detail.affected;
    if (!affected || !affected.length) return;
    box.appendChild(el('h3', { text: t('Also read by') }));
    const list = el('div', { class: 'dq-affected' });
    affected.forEach(a => {
      const row = el('div', { class: 'dq-affected-row' });
      row.appendChild(el('span', { class: 'mono', text: a.generator }));
      row.appendChild(el('span', { class: 'dq-badge dq-conf-' + a.confidence.split(':')[0], text: a.confidence }));
      (a.reports || []).forEach(r => row.appendChild(el('a', { class: 'dq-pill', href: '/d/' + r.id + '?dock=data', text: r.title })));
      list.appendChild(row);
    });
    box.appendChild(list);
  }

  function renderHistory(f, box) {
    const key = f.source + '|' + f.generator;
    const h = (DATA.history || {})[key];
    if (!h) return;
    box.appendChild(el('h3', { text: t('History') }));
    box.appendChild(el('label', { class: 'dq-lbl', text: t('File count') }));
    box.appendChild(sparkline(h.file_count));
    if (f.file && f.sheet) {
      const rk = f.file + '/' + f.sheet;
      const series = h.rows && h.rows[rk];
      if (series) {
        box.appendChild(el('label', { class: 'dq-lbl', text: t('Rows: {rk}', {rk: rk}) }));
        box.appendChild(sparkline(series));
      }
    }
  }

  function renderAckControls(f, box, setStatus) {
    box.appendChild(el('h3', { text: t('Acknowledge') }));
    if (f.acked && f.ack) {
      box.appendChild(el('div', { class: 'dq-note', text: t('Acked') + (f.ack.note ? ': ' + f.ack.note : '') }));
      const unackBtn = el('button', { class: 'btn xs', type: 'button', text: t('Unacknowledge') });
      box.appendChild(unackBtn);
      unackBtn.addEventListener('click', async () => {
        unackBtn.disabled = true; setStatus(t('Saving…'));
        let res;
        try {
          res = await fetch('/api/data-quality/acks/' + f.ack.id, { method: 'DELETE', headers: CSRF_SAFE });
        } catch (e) {
          unackBtn.disabled = false; setStatus(t('Could not reach the server.'), 'err'); return;
        }
        if (!res.ok && res.status !== 204) {
          unackBtn.disabled = false; setStatus(t('Failed ({status}).', {status: res.status}), 'err'); return;
        }
        setStatus(t('Unacknowledged.'), 'ok');
        refetch();
      });
      return;
    }
    const noteIn = el('textarea', { class: 'input sm dq-note-in', placeholder: t('Note (optional)') });
    box.appendChild(noteIn);
    const btnRow = el('div', { class: 'dq-ackrow' });
    box.appendChild(btnRow);

    const h = (DATA.history || {})[f.source + '|' + f.generator];
    const latestRun = h && h.runs && h.runs[0];

    function ack(untilRunId, untilAt) {
      return async () => {
        setStatus(t('Saving…'));
        let res;
        try {
          res = await fetch('/api/data-quality/acks', {
            method: 'POST', headers: CSRF_SAFE,
            body: JSON.stringify({
              finding_id: f.id, source_id: f.source_id,
              until_run_id: untilRunId != null ? untilRunId : null,
              until_at: untilAt || null,
              note: noteIn.value || null,
            }),
          });
        } catch (e) { setStatus(t('Could not reach the server.'), 'err'); return; }
        if (!res.ok) { setStatus(t('Failed ({status}).', {status: res.status}), 'err'); return; }
        setStatus(t('Acknowledged.'), 'ok');
        refetch();
      };
    }
    function isoIn(days) { return new Date(Date.now() + days * 86400000).toISOString(); }

    // A schema finding's `since_run` pins to the OLDEST run of the unbroken
    // reproduction, not the newest — so "until next run" (until_run_id >=
    // since_run) never actually expires it; it is effectively "forever" but
    // labelled otherwise. Keep the button for kinds it works for, and offer
    // schema kinds a dated ack instead (see quality._apply_acks docstring).
    const isSchema = (f.kind || '').indexOf('schema_') === 0;
    const buttons = [];
    if (!isSchema) {
      const untilRunBtn = el('button', { class: 'btn xs', type: 'button', text: t('until next run') });
      untilRunBtn.addEventListener('click', ack(latestRun, null));
      buttons.push(untilRunBtn);
    }
    const d7 = el('button', { class: 'btn xs', type: 'button', text: t('{n} days', {n: 7}) });
    d7.addEventListener('click', ack(null, isoIn(7)));
    const d30 = el('button', { class: 'btn xs', type: 'button', text: t('{n} days', {n: 30}) });
    d30.addEventListener('click', ack(null, isoIn(30)));
    const forever = el('button', { class: 'btn xs', type: 'button', text: t('forever') });
    forever.addEventListener('click', ack(null, null));
    buttons.push(d7, d30, forever);
    buttons.forEach(b => btnRow.appendChild(b));
  }

  // ── drawer open/close ────────────────────────────────────────────
  let lastFocus = null;
  function openDrawer(findingId) {
    const f = (DATA.findings || []).find(x => x.id === findingId);
    if (!f || !drawer) return;
    // Only capture the return-focus target on a genuinely NEW open. refetch()
    // re-runs openDrawer for the SAME finding after every drawer mutation
    // (ack, accept, etc.) to redraw it with fresh data; by then
    // document.activeElement is whatever button inside the drawer the user
    // just clicked, and renderAll() is about to tear that button out of the
    // DOM — capturing it here would make Escape's later .focus() a no-op on
    // a detached node, dropping focus to <body> instead of back to the row.
    if (state.openFinding !== findingId) lastFocus = document.activeElement;
    state.openFinding = findingId;
    drawerTitle.textContent = KIND_LABELS[f.kind] || f.kind;
    drawerBody.textContent = '';

    const head = el('div', { class: 'dq-drawer-sect' });
    head.appendChild(el('div', { class: 'dq-badge dq-sev-badge-' + f.severity, text: f.severity }));
    head.appendChild(el('p', { class: 'dq-msg', text: f.message }));
    head.appendChild(el('div', { class: 'dq-dl' }, [
      el('div', {}, [el('dt', { text: t('Source') }), el('dd', { class: 'mono', text: f.source })]),
      el('div', {}, [el('dt', { text: t('Generator') }), el('dd', { class: 'mono', text: f.generator || '—' })]),
      el('div', {}, [el('dt', { text: t('Location') }), el('dd', { class: 'mono', text: locator(f) })]),
      el('div', {}, [el('dt', { text: t('Since run') }), el('dd', { class: 'mono', text: f.since_run != null ? '#' + f.since_run : '—' })]),
    ]));
    drawerBody.appendChild(head);

    const status = el('span', { class: 'dq-status mono' });
    function setStatus(msg, kind) {
      status.textContent = msg || '';
      status.style.color = kind === 'err' ? 'var(--danger)' : kind === 'ok' ? 'var(--success)' : 'var(--ink-dim)';
    }

    const detailSect = el('div', { class: 'dq-drawer-sect' });
    if (f.kind === 'check_fail') renderCheckFail(f, detailSect);
    else if (f.kind === 'new_values') renderNewValues(f, detailSect, setStatus);
    else if (f.kind === 'schema_missing_column') renderSchemaMissingColumn(f, detailSect, setStatus);
    else if (f.kind === 'unmapped_values') renderUnmappedValues(f, detailSect);
    else if (f.kind === 'rerun_needed') renderRerunNeeded(f, detailSect);
    if (detailSect.children.length) drawerBody.appendChild(detailSect);

    const affectedSect = el('div', { class: 'dq-drawer-sect' });
    renderAffected(f, affectedSect);
    if (affectedSect.children.length) drawerBody.appendChild(affectedSect);

    const historySect = el('div', { class: 'dq-drawer-sect' });
    renderHistory(f, historySect);
    if (historySect.children.length) drawerBody.appendChild(historySect);

    const ackSect = el('div', { class: 'dq-drawer-sect' });
    renderAckControls(f, ackSect, setStatus);
    ackSect.appendChild(status);
    drawerBody.appendChild(ackSect);

    drawer.hidden = false;
    if (scrim) scrim.hidden = false;
    pushUrlFinding(f.id);
    const closeBtnEl = drawer.querySelector('[data-drawer-close]');
    if (closeBtnEl && closeBtnEl.focus) closeBtnEl.focus();
    else if (drawer.focus) drawer.focus();
  }

  function pushUrlFinding(findingId) {
    const p = new URLSearchParams(location.search);
    if (findingId) p.set('finding', findingId); else p.delete('finding');
    const qs = p.toString();
    const url = location.pathname + (qs ? '?' + qs : '');
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', url);
  }

  function closeDrawer() {
    if (!drawer) return;
    drawer.hidden = true;
    if (scrim) scrim.hidden = true;
    state.openFinding = null;
    pushUrlFinding(null);
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
    const generator = p.get('generator');
    const findingId = p.get('finding');
    if (sourceId && sourceSel) { sourceSel.value = sourceId; state.source = sourceId; }
    if (generator && genSel) { genSel.value = generator; state.generator = generator; }
    renderTable();
    renderThresholds();
    if (findingId) openDrawer(findingId);
  }

  renderAll();
  initFromUrl();
})();
