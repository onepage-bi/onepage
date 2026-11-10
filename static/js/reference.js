/* Reference tables — list page (#ref-root) and detail page (#refd-root).
 *
 * Same idiom as static/js/catalog.js: the page's initial paint comes from
 * JSON baked into a data-* attribute at render time (one request total for
 * the page load), `el()` builds every dynamic node with textContent (never
 * innerHTML), every JSON write carries `Content-Type: application/json`,
 * every upload goes through FormData. The create/settings/import FORMS
 * themselves are static markup already in the template (not built by this
 * script) so static/js/user-suggest.js — loaded just before this file —
 * can attach its typeahead to the `data-user-suggest` inputs at parse time;
 * this script only shows/hides the drawer and reads/writes those fields.
 *
 * Both pages live in one file — each guards on its own root element and
 * returns early if that root is absent, so loading this script on the
 * wrong page is a no-op.
 */
(function () {
  'use strict';

  function parseJson(raw, fallback) {
    try { return JSON.parse(raw || ''); } catch (e) { return fallback; }
  }

  const CSRF_SAFE = { 'Content-Type': 'application/json' };

  // ── tiny DOM helper, same idiom as catalog.js ────────────────────────
  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === 'text') { n.textContent = attrs[k]; continue; }
      if (k === 'class') { n.className = attrs[k]; continue; }
      if (k in n && k !== 'for') n[k] = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(c => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }

  function toast(msg, kind) {
    if (window.OP_TOAST) window.OP_TOAST(msg, kind || 'info');
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return OP_I18N.fmtDate(d, { day: '2-digit', month: '2-digit', year: 'numeric' });
  }

  const TYPE_LABELS = { text: t('text'), number: t('number {ctx:2}'), date: t('date') };

  // ═══════════════════════════════════════════════════════════════════
  // List page — #ref-root
  // ═══════════════════════════════════════════════════════════════════
  (function listPage() {
    const root = document.getElementById('ref-root');
    if (!root) return;

    let TABLES = parseJson(root.dataset.tables, []);

    const tbody = document.getElementById('ref-tbody');
    const emptyEl = document.getElementById('ref-empty');
    const createBtn = document.getElementById('ref-create-btn');
    const drawer = document.getElementById('ref-drawer');
    const scrim = document.getElementById('ref-scrim');
    const drawerClose = document.getElementById('ref-drawer-close');
    const filterSel = document.getElementById('ref-filter-gen');
    const filterClear = document.getElementById('ref-filter-clear');
    const filterCount = document.getElementById('ref-filter-count');

    // ── "Reference tables of this report" ────────────────────────────
    // One filter, two spellings of the same thing: `?generator=<name>` (what
    // a report page links with — a generator NAME is what an author knows)
    // and `?dashboard=<id>` (what a dashboard tile links with). The query
    // wins over localStorage: a link must show what it promises, whatever
    // this browser last looked at.
    const FILTER_KEY = 'op-ref-filter';

    function readStoredFilter() {
      try { return JSON.parse(localStorage.getItem(FILTER_KEY) || 'null') || null; }
      catch (e) { return null; }
    }
    function storeFilter(f) {
      try {
        if (f) localStorage.setItem(FILTER_KEY, JSON.stringify(f));
        else localStorage.removeItem(FILTER_KEY);
      } catch (e) { /* private mode: the filter just does not persist */ }
    }
    function queryFilter() {
      const q = String((location.search || '')).replace(/^\?/, '');
      const out = {};
      q.split('&').forEach(pair => {
        if (!pair) return;
        const i = pair.indexOf('=');
        const k = decodeURIComponent(i < 0 ? pair : pair.slice(0, i));
        const v = decodeURIComponent((i < 0 ? '' : pair.slice(i + 1)).replace(/\+/g, ' '));
        if (k === 'generator' && v) out.generator = v;
        if (k === 'dashboard' && v) out.dashboard = v;
      });
      return (out.generator || out.dashboard) ? out : null;
    }

    let FILTER = queryFilter() || readStoredFilter();

    function matchesFilter(tv) {
      if (!FILTER) return true;
      if (FILTER.generator) {
        return (tv.consumer_generators || []).some(
          g => String(g.name) === String(FILTER.generator) || String(g.id) === String(FILTER.generator));
      }
      if (FILTER.dashboard) {
        return (tv.consumer_dashboards || []).some(d => String(d.id) === String(FILTER.dashboard));
      }
      return true;
    }

    function syncUrl() {
      if (!window.history || !history.replaceState) return;
      const base = location.pathname;
      if (FILTER && FILTER.generator) history.replaceState(null, '', base + '?generator=' + encodeURIComponent(FILTER.generator));
      else if (FILTER && FILTER.dashboard) history.replaceState(null, '', base + '?dashboard=' + encodeURIComponent(FILTER.dashboard));
      else history.replaceState(null, '', base);
    }

    function renderFilter(shown) {
      if (filterSel) {
        const names = [];
        TABLES.forEach(tv => (tv.consumer_generators || []).forEach(g => {
          if (names.indexOf(g.name) < 0) names.push(g.name);
        }));
        names.sort();
        filterSel.textContent = '';
        filterSel.appendChild(el('option', { value: '', text: t('All reference tables') }));
        names.forEach(n => filterSel.appendChild(el('option', { value: n, text: n })));
        const want = (FILTER && FILTER.generator) || '';
        // A link to ?generator=X where X consumes nothing this user can read:
        // showing "All reference tables" while the list is empty would read as a
        // broken page. Name X, disabled, so the select and the table agree.
        if (want && names.indexOf(want) < 0) {
          filterSel.appendChild(el('option', {
            value: want, disabled: true, text: t('{p1} — no reference tables available', {p1: want}) }));
        }
        filterSel.value = want;
      }
      if (filterClear) filterClear.hidden = !FILTER;
      if (filterCount) {
        filterCount.textContent = FILTER
          ? t('{p1} of {p2}', {p1: shown, p2: TABLES.length})
          : '';
      }
    }

    function setFilter(f) {
      FILTER = f;
      storeFilter(f);
      syncUrl();
      render();
    }

    if (filterSel) {
      filterSel.addEventListener('change', () => {
        setFilter(filterSel.value ? { generator: filterSel.value } : null);
      });
    }
    if (filterClear) filterClear.addEventListener('click', () => setFilter(null));

    function refetch() {
      return fetch('/api/reference')
        .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(json => { TABLES = json; render(); });
    }

    function render() {
      tbody.textContent = '';
      const shown = TABLES.filter(matchesFilter);
      shown.forEach(tv => {
        const tr = el('tr', {});
        const nameCell = el('td', {});
        nameCell.appendChild(el('a', { href: '/reference/' + encodeURIComponent(tv.slug), text: tv.title }));
        tr.appendChild(nameCell);
        tr.appendChild(el('td', { class: 'mono', text: tv.slug }));
        tr.appendChild(el('td', { class: 'mono', text: tv.key_column || '—' }));
        tr.appendChild(el('td', { class: 'numeric mono', text: String((tv.columns || []).length) }));
        const rowsCell = el('td', { class: 'numeric mono', text: String(tv.row_count != null ? tv.row_count : 0) });
        if (!(tv.current_version || 0)) {
          // Created from a manifest, never imported into: the row would
          // otherwise read as a plain "0" with no hint of what to do.
          rowsCell.textContent = '0';
          rowsCell.appendChild(document.createTextNode(' '));
          rowsCell.appendChild(el('a', {
            class: 'ref-note',
            href: '/api/reference/' + encodeURIComponent(tv.slug) + '/template',
            text: t('template'),
          }));
        }
        tr.appendChild(rowsCell);
        tr.appendChild(el('td', { class: 'numeric mono', text: 'v' + (tv.current_version != null ? tv.current_version : 0) }));
        tr.appendChild(el('td', { text: (tv.responsible && tv.responsible.display_name) || t('not assigned') }));
        tr.appendChild(el('td', { class: 'numeric mono', text: String(tv.consumers != null ? tv.consumers : 0) }));
        tr.appendChild(el('td', { class: 'mono', text: fmtDate(tv.updated_at) }));
        tbody.appendChild(tr);
      });
      if (emptyEl) emptyEl.hidden = shown.length !== 0;
      renderFilter(shown.length);
    }

    // ── create drawer (static form in the template) ──────────────────
    const slugIn = document.getElementById('ref-c-slug');
    const titleIn = document.getElementById('ref-c-title');
    const descIn = document.getElementById('ref-c-desc');
    const respIn = document.getElementById('ref-c-resp');
    const pinCk = document.getElementById('ref-c-pin');
    const modeManual = document.getElementById('ref-c-mode-manual');
    const modeGen = document.getElementById('ref-c-mode-gen');
    const manualBox = document.getElementById('ref-c-manual-box');
    const genBox = document.getElementById('ref-c-gen-box');
    const columnsBox = document.getElementById('ref-c-columns');
    const addColBtn = document.getElementById('ref-c-add-col');
    const genSel = document.getElementById('ref-c-gen-sel');
    const submitBtn = document.getElementById('ref-c-submit');
    const statusEl = document.getElementById('ref-c-status');

    const columnRows = [];
    function addColumnRow() {
      if (!columnsBox) return;
      const row = el('div', { class: 'ref-inline-row' });
      const nameIn = el('input', { class: 'input sm', type: 'text', placeholder: t('column name') });
      const typeSel = el('select', { class: 'input sm' });
      ['text', 'number', 'date'].forEach(tv => typeSel.appendChild(el('option', { value: tv, text: TYPE_LABELS[tv] })));
      row.appendChild(nameIn);
      row.appendChild(typeSel);
      columnsBox.appendChild(row);
      columnRows.push({ nameIn, typeSel });
    }
    if (columnsBox) { addColumnRow(); addColumnRow(); }
    if (addColBtn) addColBtn.addEventListener('click', addColumnRow);

    if (genSel) {
      fetch('/api/generators').then(r => r.ok ? r.json() : []).then(gens => {
        (gens || []).forEach(g => genSel.appendChild(el('option', { value: String(g.id), text: g.name })));
      }).catch(() => {});
    }

    function setColMode(useGen) {
      if (manualBox) manualBox.hidden = useGen;
      if (genBox) genBox.hidden = !useGen;
    }
    if (modeManual) modeManual.addEventListener('change', () => setColMode(false));
    if (modeGen) modeGen.addEventListener('change', () => setColMode(true));

    function resetCreateForm() {
      if (slugIn) slugIn.value = '';
      if (titleIn) titleIn.value = '';
      if (descIn) descIn.value = '';
      if (respIn) respIn.value = '';
      if (pinCk) pinCk.checked = false;
      if (statusEl) statusEl.textContent = '';
      columnRows.forEach(r => { r.nameIn.value = ''; r.typeSel.value = 'text'; });
      if (modeManual) { modeManual.checked = true; setColMode(false); }
      if (submitBtn) submitBtn.disabled = false;
    }

    function openDrawer() {
      if (!drawer) return;
      resetCreateForm();
      drawer.hidden = false;
      if (scrim) scrim.hidden = false;
    }
    function closeDrawer() {
      if (!drawer) return;
      drawer.hidden = true;
      if (scrim) scrim.hidden = true;
    }
    if (createBtn) createBtn.addEventListener('click', openDrawer);
    if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
    if (scrim) scrim.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && drawer && !drawer.hidden) closeDrawer();
    });

    if (submitBtn) submitBtn.addEventListener('click', async () => {
      const slug = (slugIn.value || '').trim();
      const title = (titleIn.value || '').trim();
      if (!slug) { statusEl.textContent = t('Enter a slug.'); return; }
      if (!title) { statusEl.textContent = t('Enter a name.'); return; }

      const body = {
        slug, title,
        description: (descIn.value || '').trim() || null,
        allow_pin: !!pinCk.checked,
      };
      // The typeahead (user-suggest.js) fills this field with an PERSON_ID,
      // not a numeric users.id — `Number('kvf027')` is NaN, and treating that
      // as «nobody» silently dropped every person picked from the list. Send
      // whichever shape the value actually is and let the server resolve it.
      const respVal = (respIn.value || '').trim();
      if (respVal) {
        if (/^\d+$/.test(respVal)) body.responsible_user_id = Number(respVal);
        else body.responsible_person_id = respVal;
      }

      if (modeGen && modeGen.checked) {
        if (!genSel.value) { statusEl.textContent = t('Select a generator.'); return; }
        body.from_generator_id = Number(genSel.value);
      } else {
        const cols = columnRows
          .map(r => ({ name: (r.nameIn.value || '').trim(), type: r.typeSel.value }))
          .filter(c => c.name);
        if (cols.length < 2) { statusEl.textContent = t('At least 2 columns are needed.'); return; }
        body.columns = cols;
      }

      statusEl.textContent = t('Creating…');
      submitBtn.disabled = true;
      let res;
      try {
        res = await fetch('/api/reference', { method: 'POST', headers: CSRF_SAFE, body: JSON.stringify(body) });
      } catch (e) {
        statusEl.textContent = t('Could not reach the server.'); submitBtn.disabled = false; return;
      }
      if (res.status === 409) { statusEl.textContent = t('That slug is already taken.'); submitBtn.disabled = false; return; }
      if (res.status === 403) { statusEl.textContent = t('You don\'t have permission to create a reference table.'); submitBtn.disabled = false; return; }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        statusEl.textContent = t('Error: {p1}', {p1: (d.detail || res.status)});
        submitBtn.disabled = false;
        return;
      }
      const created = await res.json();
      toast(t('Reference table created'), 'success');
      location.href = '/reference/' + encodeURIComponent(created.slug);
    });

    render();
  })();

  // ═══════════════════════════════════════════════════════════════════
  // Detail page — #refd-root
  // ═══════════════════════════════════════════════════════════════════
  (function detailPage() {
    const root = document.getElementById('refd-root');
    if (!root) return;

    const SLUG = root.dataset.slug;
    let DATA = parseJson(root.dataset.table, {});

    const slugChip = document.getElementById('refd-slug-chip');
    const titleEl = document.getElementById('refd-title');
    const metaEl = document.getElementById('refd-meta');
    const exportBtn = document.getElementById('refd-export-btn');
    const importBtn = document.getElementById('refd-import-btn');
    const settingsBtn = document.getElementById('refd-settings-btn');
    const consumersEl = document.getElementById('refd-consumers');
    const unmappedEl = document.getElementById('refd-unmapped');
    const searchIn = document.getElementById('refd-search');
    const pageInfoEl = document.getElementById('refd-page-info');
    const dataThead = document.getElementById('refd-data-thead');
    const dataTbody = document.getElementById('refd-data-tbody');
    const pagePrev = document.getElementById('refd-page-prev');
    const pageNext = document.getElementById('refd-page-next');
    const versionsTbody = document.getElementById('refd-versions-tbody');
    const deleteBtn = document.getElementById('refd-delete-btn');
    const emptyBanner = document.getElementById('refd-empty-banner');
    const emptyText = document.getElementById('refd-empty-text');
    const templateBtn = document.getElementById('refd-template-btn');

    const drawer = document.getElementById('refd-drawer');
    const scrim = document.getElementById('refd-scrim');
    const drawerTitle = document.getElementById('refd-drawer-title');
    const settingsForm = document.getElementById('refd-settings-form');
    const importForm = document.getElementById('refd-import-form');
    const drawerClose = document.getElementById('refd-drawer-close');

    let pageOffset = 0;
    const PAGE_SIZE = 500;
    let searchQ = '';

    function openDrawer(title, formEl) {
      if (!drawer) return;
      drawerTitle.textContent = title;
      if (settingsForm) settingsForm.hidden = (formEl !== settingsForm);
      if (importForm) importForm.hidden = (formEl !== importForm);
      drawer.hidden = false;
      if (scrim) scrim.hidden = false;
    }
    function closeDrawer() {
      if (!drawer) return;
      drawer.hidden = true;
      if (scrim) scrim.hidden = true;
    }
    if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
    if (scrim) scrim.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && drawer && !drawer.hidden) closeDrawer();
    });

    function renderHead() {
      if (slugChip) slugChip.textContent = SLUG;
      if (titleEl) titleEl.textContent = DATA.title || SLUG;
      if (metaEl) {
        const respName = (DATA.responsible && DATA.responsible.display_name) || t('not assigned');
        metaEl.textContent = t('v{p1} · {p2} rows · updated {p3} · owner {p4}', {p1: (DATA.current_version || 0), p2: (DATA.row_total != null ? DATA.row_total : 0), p3: fmtDate(DATA.updated_at), p4: respName});
      }
      if (exportBtn) exportBtn.href = '/api/reference/' + encodeURIComponent(SLUG) + '/versions/current/xlsx';
      renderEmptyBanner();
    }

    // "Created from manifest X · 0 rows — upload Excel". Shown only while
    // the table has never been imported into: after v1 it would be noise.
    function renderEmptyBanner() {
      if (!emptyBanner) return;
      const empty = !(DATA.current_version || 0);
      emptyBanner.hidden = !empty;
      if (!empty) return;
      // From the DECLARATIONS, not from the consumer rows: a generator with
      // no dashboard yet has no consumer row, and "Created from manifest …"
      // would then name nobody on exactly the table it just created.
      const gens = [];
      (DATA.consumer_generators || []).forEach(g => {
        if (g.name && gens.indexOf(g.name) < 0) gens.push(g.name);
      });
      (DATA.consumers || []).forEach(c => {
        if (c.generator_name && gens.indexOf(c.generator_name) < 0) gens.push(c.generator_name);
      });
      if (emptyText) {
        emptyText.textContent = (gens.length ? t('Created from manifest {p1} · ', {p1: gens.join(', ')}) : '')
          + t('rows 0 — load Excel');
      }
      if (templateBtn) templateBtn.href = '/api/reference/' + encodeURIComponent(SLUG) + '/template';
    }

    if (deleteBtn) {
      deleteBtn.addEventListener('click', () => {
        // Destructive and unrecoverable: every version goes with the table.
        // Confirmation names the table so a mis-click on the wrong tab is
        // visible before it happens.
        if (!window.confirm(t('Delete the reference table “{p1}” along with its whole version history? This can\'t be undone.', {p1: (DATA.title || SLUG)}))) return;
        fetch('/api/reference/' + encodeURIComponent(SLUG), { method: 'DELETE' })
          .then(r => {
            if (!r.ok && r.status !== 204) throw new Error(String(r.status));
            location.href = '/reference';
          })
          .catch(e => toast(t('Couldn\'t delete: {p1}', {p1: e.message}), 'error'));
      });
    }

    function renderConsumers() {
      if (!consumersEl) return;
      consumersEl.textContent = '';
      const list = DATA.consumers || [];
      if (!list.length) {
        consumersEl.appendChild(el('div', { class: 'ref-note', text: t('No consumers yet.') }));
        return;
      }
      const tbl = el('table', { class: 'tbl ref-table' });
      const thead = el('thead', {}, [el('tr', {}, [
        el('th', { text: t('Report') }), el('th', { text: t('Generator') }), el('th', { text: t('Used') }),
      ])]);
      tbl.appendChild(thead);
      const tb = el('tbody');
      list.forEach(c => {
        const tr = el('tr', {});
        const reportCell = el('td', {});
        reportCell.appendChild(el('a', { href: '/d/' + encodeURIComponent(c.dashboard_id), text: c.title || c.dashboard_id }));
        tr.appendChild(reportCell);
        const genCell = el('td', { text: c.generator_name || '—' });
        // A report may consume a dictionary in a RUN, in the chat, or both.
        // "in chat" is the case that would otherwise look like "nobody uses
        // this table": its semantics read it, but no run ever pinned it.
        const via = c.via || ['run'];
        if (via.indexOf('chat') >= 0) {
          genCell.appendChild(document.createTextNode(' '));
          genCell.appendChild(el('span', { class: 'badge', text: t('in chat') }));
        }
        tr.appendChild(genCell);
        const useCell = el('td', {});
        const lr = c.last_run;
        if (!lr && via.length === 1 && via[0] === 'chat') {
          // Not a run consumer at all: "never run" would read as a
          // problem, and there is nothing to run here.
          useCell.appendChild(el('span', { class: 'ref-note', text: t('reads in chat') }));
        } else if (!lr) {
          useCell.appendChild(el('span', { class: 'ref-note', text: t('hasn\'t run yet') }));
        } else if (!lr.applied) {
          useCell.appendChild(el('span', { class: 'ref-note', text: t('without reference table') }));
        } else {
          useCell.appendChild(el('span', { text: t('used v{p1}', {p1: lr.version_number}) }));
          if (lr.version_number != null && lr.version_number < (DATA.current_version || 0)) {
            useCell.appendChild(el('span', { class: 'badge warning', text: t('recalculation needed') }));
          }
        }
        tr.appendChild(useCell);
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      consumersEl.appendChild(tbl);
    }

    function renderUnmapped() {
      if (!unmappedEl) return;
      unmappedEl.textContent = '';
      unmappedEl.appendChild(el('div', { class: 'ref-note', text: t('Loading…') }));
      fetch('/api/data-quality')
        .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(json => {
          const findings = (json && json.findings) || [];
          const mine = findings.filter(f => f && f.kind === 'unmapped_values' &&
            f.detail && f.detail.table === SLUG);
          unmappedEl.textContent = '';
          if (!mine.length) {
            unmappedEl.appendChild(el('div', { class: 'ref-note', text: t('There are no open mismatches.') }));
            return;
          }
          mine.forEach(f => {
            const row = el('div', { class: 'ref-unmapped-row' });
            const dashIds = f.dashboard_ids || [];
            if (dashIds.length) {
              row.appendChild(el('a', {
                href: '/d/' + encodeURIComponent(dashIds[0]),
                text: f.message || (f.generator || t('report')),
              }));
            } else {
              row.appendChild(el('span', { text: f.message || (f.generator || '—') }));
            }
            const tplUrl = f.detail && f.detail.template_url;
            if (tplUrl) {
              row.appendChild(el('a', { class: 'btn xs', href: tplUrl, text: t('Download new values template') }));
            }
            unmappedEl.appendChild(row);
          });
        })
        .catch(() => {
          unmappedEl.textContent = '';
          unmappedEl.appendChild(el('div', { class: 'ref-note', text: t('Couldn\'t load mismatches.') }));
        });
    }

    function renderDataGrid() {
      if (!dataThead || !dataTbody) return;
      const columns = DATA.columns || [];
      dataThead.textContent = '';
      columns.forEach(c => dataThead.appendChild(el('th', { text: c.name })));

      const q = searchQ.trim().toLowerCase();
      const rows = DATA.rows || [];
      let filtered = rows;
      if (q) {
        filtered = rows.filter(r => r.length && String(r[0] == null ? '' : r[0]).toLowerCase().includes(q));
      }
      const shown = filtered.slice(0, PAGE_SIZE);

      dataTbody.textContent = '';
      shown.forEach(r => {
        const tr = el('tr', {});
        columns.forEach((c, i) => {
          const v = r[i];
          const cls = c.type === 'number' ? 'numeric mono' : 'mono';
          tr.appendChild(el('td', { class: cls, text: v == null ? '' : String(v) }));
        });
        dataTbody.appendChild(tr);
      });

      const total = DATA.row_total != null ? DATA.row_total : rows.length;
      if (pageInfoEl) {
        pageInfoEl.textContent = q
          ? (t('{p1} of {p2} on page (search)', {p1: filtered.length, p2: rows.length}))
          : (t('rows {p1}–{p2} of {p3}', {p1: (rows.length ? (pageOffset + 1) : 0), p2: (pageOffset + rows.length), p3: total}));
      }
      if (pagePrev) pagePrev.disabled = pageOffset <= 0;
      if (pageNext) pageNext.disabled = (pageOffset + rows.length) >= total;
    }

    function loadPage(offset) {
      return fetch('/api/reference/' + encodeURIComponent(SLUG) + '?offset=' + offset + '&limit=' + PAGE_SIZE)
        .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(json => {
          DATA = Object.assign({}, DATA, json);
          pageOffset = offset;
          renderHead();
          renderDataGrid();
        })
        .catch(() => toast(t('Couldn\'t load the data page'), 'error'));
    }

    if (pagePrev) pagePrev.addEventListener('click', () => {
      if (pageOffset > 0) loadPage(Math.max(0, pageOffset - PAGE_SIZE));
    });
    if (pageNext) pageNext.addEventListener('click', () => {
      loadPage(pageOffset + PAGE_SIZE);
    });
    if (searchIn) searchIn.addEventListener('input', () => {
      searchQ = searchIn.value; renderDataGrid();
    });

    function renderVersions() {
      if (!versionsTbody) return;
      versionsTbody.textContent = '';
      (DATA.versions || []).forEach(v => {
        const tr = el('tr', {});
        tr.appendChild(el('td', { class: 'numeric mono', text: String(v.number) }));
        tr.appendChild(el('td', { class: 'numeric mono', text: String(v.row_count) }));
        const summary = v.summary || {};
        const diffCell = el('td', { class: 'ref-diff-pills' });
        if (summary.added) diffCell.appendChild(el('span', { class: 'badge success', text: '+' + summary.added }));
        if (summary.changed) diffCell.appendChild(el('span', { class: 'badge warning', text: '~' + summary.changed }));
        if (summary.removed) diffCell.appendChild(el('span', { class: 'badge danger', text: '−' + summary.removed }));
        if (!summary.added && !summary.changed && !summary.removed) diffCell.appendChild(el('span', { class: 'ref-note', text: '—' }));
        tr.appendChild(diffCell);
        tr.appendChild(el('td', { text: v.created_by || '—' }));
        tr.appendChild(el('td', { class: 'mono', text: fmtDate(v.created_at) }));
        tr.appendChild(el('td', { text: v.note || '' }));
        const actionCell = el('td', {});
        if (DATA.can_write) {
          const restoreBtn = el('button', { class: 'btn xs ghost', type: 'button', text: t('Restore') });
          restoreBtn.addEventListener('click', async () => {
            if (!confirm(t('Make version {p1} current? This will create a new version with the same data.', {p1: v.number}))) return;
            let res;
            try {
              res = await fetch('/api/reference/' + encodeURIComponent(SLUG) + '/versions/' + v.number + '/restore',
                { method: 'POST', headers: CSRF_SAFE });
            } catch (e) { toast(t('Couldn\'t reach the server'), 'error'); return; }
            if (!res.ok) { toast(t('Couldn\'t restore the version'), 'error'); return; }
            const rr = await res.json();
            toast(t('Version {p1} created', {p1: rr.version}), 'success');
            location.reload();
          });
          actionCell.appendChild(restoreBtn);
        }
        tr.appendChild(actionCell);
        versionsTbody.appendChild(tr);
      });
    }

    // ── settings drawer (static form in the template) ────────────────
    const sTitleIn = document.getElementById('refd-s-title');
    const sDescIn = document.getElementById('refd-s-desc');
    const sRespIn = document.getElementById('refd-s-resp');
    const sPinCk = document.getElementById('refd-s-pin');
    const sSubmit = document.getElementById('refd-s-submit');
    const sStatus = document.getElementById('refd-s-status');

    if (sSubmit) sSubmit.addEventListener('click', async () => {
      const body = {
        title: (sTitleIn.value || '').trim(),
        description: (sDescIn.value || '').trim() || null,
        allow_pin: !!sPinCk.checked,
      };
      // The typeahead (user-suggest.js) fills this field with an PERSON_ID,
      // not a numeric users.id — `Number('kvf027')` is NaN, and treating that
      // as «nobody» silently dropped every person picked from the list. Send
      // whichever shape the value actually is and let the server resolve it.
      const respVal = (sRespIn.value || '').trim();
      if (respVal) {
        if (/^\d+$/.test(respVal)) body.responsible_user_id = Number(respVal);
        else body.responsible_person_id = respVal;
      } else {
        body.responsible_user_id = null;
      }
      sStatus.textContent = t('Saving…');
      let res;
      try {
        res = await fetch('/api/reference/' + encodeURIComponent(SLUG),
          { method: 'PUT', headers: CSRF_SAFE, body: JSON.stringify(body) });
      } catch (e) { sStatus.textContent = t('Could not reach the server.'); return; }
      if (!res.ok) { sStatus.textContent = t('Error ({p1}).', {p1: res.status}); return; }
      toast(t('Saved'), 'success');
      location.reload();
    });

    // ── import drawer (static form in the template) ──────────────────
    const iFile = document.getElementById('refd-i-file');
    const iDelete = document.getElementById('refd-i-delete');
    const iNote = document.getElementById('refd-i-note');
    const iResult = document.getElementById('refd-i-result');
    const iCheck = document.getElementById('refd-i-check');
    const iApply = document.getElementById('refd-i-apply');
    const iStatus = document.getElementById('refd-i-status');

    let lastDryRun = null;

    function renderDryRun(d) {
      iResult.textContent = '';
      const pills = el('div', { class: 'ref-diff-pills' });
      pills.appendChild(el('span', { class: 'badge success', text: t('added: {p1}', {p1: (d.added || 0)}) }));
      pills.appendChild(el('span', { class: 'badge warning', text: t('changed: {p1}', {p1: (d.changed || 0)}) }));
      pills.appendChild(el('span', { class: 'badge danger', text: t('deleted: {p1}', {p1: (d.removed || 0)}) }));
      pills.appendChild(el('span', { class: 'badge', text: t('unchanged: {p1}', {p1: (d.unchanged || 0)}) }));
      iResult.appendChild(pills);

      const errs = d.errors || [];
      if (errs.length) {
        const tbl = el('table', { class: 'tbl ref-table' });
        tbl.appendChild(el('thead', {}, [el('tr', {}, [
          el('th', { text: t('Row') }), el('th', { text: t('Column') }), el('th', { text: t('Reason') }),
        ])]));
        const tb = el('tbody');
        errs.forEach(e => {
          tb.appendChild(el('tr', {}, [
            el('td', { class: 'mono', text: String(e.row != null ? e.row : '') }),
            el('td', { class: 'mono', text: e.column || '' }),
            el('td', { text: e.reason || '' }),
          ]));
        });
        tbl.appendChild(tb);
        iResult.appendChild(tbl);
      }
      const dups = d.duplicates || [];
      if (dups.length) {
        iResult.appendChild(el('div', { class: 'ref-note', text: t('Duplicate keys: {p1}', {p1: dups.join(', ')}) }));
      }
    }

    if (iCheck) iCheck.addEventListener('click', async () => {
      const file = iFile.files && iFile.files[0];
      if (!file) { iStatus.textContent = t('Select a file.'); return; }
      iStatus.textContent = t('Validating…');
      iApply.disabled = true;
      const fd = new FormData();
      fd.append('file', file);
      fd.append('dry_run', '1');
      fd.append('expected_version', String(DATA.current_version || 0));
      fd.append('delete_missing', iDelete.checked ? '1' : '0');
      if ((iNote.value || '').trim()) fd.append('note', iNote.value.trim());
      let res;
      try {
        res = await fetch('/api/reference/' + encodeURIComponent(SLUG) + '/import', { method: 'POST', body: fd });
      } catch (e) { iStatus.textContent = t('Could not reach the server.'); return; }
      if (!res.ok) { iStatus.textContent = t('Validation error ({p1}).', {p1: res.status}); return; }
      const d = await res.json();
      lastDryRun = d;
      renderDryRun(d);
      iApply.disabled = !d.can_commit;
      iStatus.textContent = d.can_commit ? t('Ready to apply.') : t('There are errors — fix the file and check again.');
    });

    if (iApply) iApply.addEventListener('click', async () => {
      const file = iFile.files && iFile.files[0];
      if (!file || !lastDryRun) { iStatus.textContent = t('Check the file first.'); return; }
      iStatus.textContent = t('Applying…');
      iApply.disabled = true;
      const fd = new FormData();
      fd.append('file', file);
      fd.append('dry_run', '0');
      fd.append('expected_version', String(DATA.current_version || 0));
      fd.append('delete_missing', iDelete.checked ? '1' : '0');
      if ((iNote.value || '').trim()) fd.append('note', iNote.value.trim());
      let res;
      try {
        res = await fetch('/api/reference/' + encodeURIComponent(SLUG) + '/import', { method: 'POST', body: fd });
      } catch (e) { iStatus.textContent = t('Could not reach the server.'); iApply.disabled = false; return; }
      if (res.status === 409) {
        toast(t('The reference table has changed, check it again'), 'error');
        iStatus.textContent = t('The reference table has changed, check it again.');
        return;
      }
      if (!res.ok) { iStatus.textContent = t('Error ({p1}).', {p1: res.status}); iApply.disabled = false; return; }
      const rr = await res.json();
      toast(t('Version {p1} created', {p1: rr.version}), 'success');
      location.reload();
    });

    function resetImportForm() {
      if (iFile) iFile.value = '';
      if (iDelete) iDelete.checked = false;
      if (iNote) iNote.value = '';
      if (iResult) iResult.textContent = '';
      if (iStatus) iStatus.textContent = '';
      if (iApply) iApply.disabled = true;
      lastDryRun = null;
    }

    if (importBtn) importBtn.addEventListener('click', () => { resetImportForm(); openDrawer(t('Import from Excel'), importForm); });
    if (settingsBtn) settingsBtn.addEventListener('click', () => openDrawer(t('Settings'), settingsForm));

    renderHead();
    renderConsumers();
    renderUnmapped();
    renderDataGrid();
    renderVersions();
  })();
})();
