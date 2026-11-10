/* Data Sources — filters and the source editor drawer.
 *
 * Two jobs that share one state: which sources are currently in view, and which
 * one is open for editing. Kept out of data-sources.js so the lineage canvas
 * stays a pure drawing concern; they meet through two events on the document,
 * `ds:filter` and `ds:open`.
 *
 * The editor's point is the live rule preview. Typing `*_template.xlsx` should
 * immediately name the files it drops, because an exclusion rule that silently
 * removes the wrong file produces a report that is quietly wrong — the failure
 * mode this whole page exists to prevent.
 */
(function () {
  'use strict';

  const root = document.getElementById('ds-root');
  if (!root) return;

  let SOURCES = [];
  try { SOURCES = JSON.parse(root.dataset.sources || '[]'); } catch (e) { return; }
  const REPORT_TITLES = (function () {
    let m = {};
    try { m = JSON.parse(root.dataset.reportTitles || '{}'); } catch (e) { /* keep {} */ }
    return m;
  })();
  const byKey = new Map(SOURCES.map(s => [s.key, s]));

  const CSRF_SAFE = { 'Content-Type': 'application/json' };

  // ── Glob matching, identical in behaviour to the engine's ──────────
  // Deliberately duplicated rather than shared: gen-engine.js runs inside the
  // report viewer and this runs on the catalogue page, and a preview that
  // disagrees with the engine would be worse than no preview. Any change here
  // must be made there too — see globToRe in static/js/gen-engine.js.
  const globCache = new Map();
  function globToRe(pat) {
    let re = globCache.get(pat);
    if (re) return re;
    let out = '';
    for (let i = 0; i < pat.length; i++) {
      const c = pat[i];
      if (c === '*') {
        if (pat[i + 1] === '*') { out += '.*'; i++; } else out += '[^/]*';
      } else if (c === '?') {
        out += '[^/]';
      } else {
        out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
      }
    }
    re = new RegExp('^' + out + '$', 'i');
    globCache.set(pat, re);
    return re;
  }
  function matchedBy(rules, name) {
    for (const p of rules) if (globToRe(p).test(name)) return p;
    return null;
  }

  // — Annotations (P3.6): the flat list of things a source's declared inputs
  // can be annotated ABOUT — one row per sheet (column=null, "the sheet as a
  // whole") plus one row per declared column, deduped across generators that
  // share the same match/sheet/column. `hint` carries the manifest author's
  // own description (from generator_column_descriptions), shown as a
  // placeholder so an unannotated column still tells you something — an
  // annotation, once saved, is the data OWNER's word and takes precedence.
  function annotationTargets(s) {
    const out = [];
    const seen = new Set();
    Object.entries(s.generator_inputs || {}).forEach(([gname, inputs]) => {
      const descMap = (s.generator_column_descriptions || {})[gname] || {};
      (inputs || []).forEach(item => {
        const match = item.match;
        const sheets = (item.sheets && item.sheets.length) ? item.sheets : [{ name: '*', columns: [] }];
        sheets.forEach(sh => {
          const sheet = sh.name || '*';
          const sheetKey = match + '|' + sheet + '|';
          if (!seen.has(sheetKey)) {
            seen.add(sheetKey);
            out.push({ match: match, sheet: sheet, column: null, hint: null });
          }
          (sh.columns || []).forEach(col => {
            const key = match + '|' + sheet + '|' + col;
            if (seen.has(key)) return;
            seen.add(key);
            const hint = (descMap[match + '/' + sheet] || {})[col] || null;
            out.push({ match: match, sheet: sheet, column: col, hint: hint });
          });
        });
      });
    });
    return out;
  }

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === 'class') n.className = attrs[k];
      else if (k === 'text') n.textContent = attrs[k];
      else if (k in n) n[k] = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(c => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }

  // ── Filters ────────────────────────────────────────────────────────
  const search = document.getElementById('ds-search');
  const chips = Array.from(document.querySelectorAll('[data-ds-chip]'));
  let facet = 'all';

  function visible(s) {
    if (facet === 'folder' && s.kind !== 'folder') return false;
    if (facet === 'greenplum' && s.kind !== 'greenplum') return false;
    // ADR-16 reference tables are a source kind like any other here.
    if (facet === 'table' && s.kind !== 'table') return false;
    if (facet === 'shared' && !s.shared) return false;
    if (facet === 'stale' && !s.stale) return false;
    if (facet === 'unclaimed' && (s.kind !== 'folder' || s.declared_path)) return false;
    const q = (search && search.value || '').trim().toLowerCase();
    if (!q) return true;
    if (s.name.toLowerCase().includes(q)) return true;
    if (s.declared_path && s.declared_path.toLowerCase().includes(q)) return true;
    if (s.label && s.label.toLowerCase().includes(q)) return true;
    // Searching by report title is how people actually look: "which folder does
    // the SL report use" is the same question from the other end.
    return (s.reports || []).some(id => (REPORT_TITLES[id] || '').toLowerCase().includes(q));
  }

  function applyFilter() {
    const live = new Set();
    SOURCES.forEach(s => { if (visible(s)) live.add(s.key); });
    document.querySelectorAll('[data-src-row]').forEach(tr => {
      tr.hidden = !live.has(tr.dataset.srcRow);
    });
    const empty = document.getElementById('ds-empty');
    if (empty) empty.hidden = live.size !== 0;
    document.dispatchEvent(new CustomEvent('ds:filter', { detail: { keys: live } }));
  }

  if (search) search.addEventListener('input', applyFilter);
  chips.forEach(c => c.addEventListener('click', () => {
    facet = c.dataset.dsChip;
    chips.forEach(o => o.classList.toggle('active', o === c));
    applyFilter();
  }));

  // ── Drawer ─────────────────────────────────────────────────────────
  const drawer = document.getElementById('ds-drawer');
  const scrim = document.getElementById('ds-scrim');
  if (!drawer) return;
  const bodyEl = drawer.querySelector('[data-drawer-body]');
  const titleEl = drawer.querySelector('[data-drawer-title]');
  let openKey = null;
  let lastFocus = null;

  function close() {
    drawer.hidden = true; scrim.hidden = true; openKey = null;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  scrim.addEventListener('click', close);
  drawer.querySelector('[data-drawer-close]').addEventListener('click', close);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !drawer.hidden) close();
  });

  function fmtBytes(b) {
    if (!b) return '—';
    const mb = b / 1048576;
    return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : Math.round(mb) + ' MB';
  }
  function fmtAge(d) {
    if (d === null || d === undefined) return '—';
    if (d === 0) return 'today';
    if (d === 1) return 'yesterday';
    return d < 30 ? d + 'd ago' : Math.round(d / 30) + 'mo ago';
  }

  function open(key, trigger) {
    const s = byKey.get(key);
    if (!s) return;
    openKey = key;
    lastFocus = trigger || document.activeElement;
    titleEl.textContent = s.name;
    bodyEl.textContent = '';

    const isFolder = s.kind === 'folder';

    // — Facts —
    const facts = el('div', { class: 'ds-dl' });
    [['Kind', isFolder ? 'Folder' : 'GreenPlum connection'],
     ['Read by', s.report_count + (s.report_count === 1 ? ' report' : ' reports')],
     ['Files', isFolder ? (s.files || '—') : '—'],
     ['Volume', isFolder ? fmtBytes(s.bytes) : '—'],
     ['Data changed', fmtAge(s.data_age_days)],
     ['Last read', fmtAge(s.read_age_days)]].forEach(([k, v]) => {
      facts.appendChild(el('div', {}, [
        el('dt', { text: k }), el('dd', { class: 'mono', text: String(v) }),
      ]));
    });
    bodyEl.appendChild(facts);

    // — Data Quality link —
    if (s.source_id) {
      const dqLink = el('div', { class: 'ds-drawer-sect' });
      dqLink.appendChild(el('a', {
        href: '/data-quality?source_id=' + encodeURIComponent(s.source_id),
        style: 'display:inline-flex;align-items:center;gap:6px;color:var(--accent);text-decoration:none;font-size:12px;font-weight:500',
        text: 'Open in Data Quality'
      }));
      bodyEl.appendChild(dqLink);
    }

    // — Reach. Shown before the fields, not after: every edit below lands on
    //   all of these reports, and that should be known before typing. —
    const reach = el('div', { class: 'ds-drawer-sect' });
    reach.appendChild(el('h3', { text: 'Feeds' }));
    const pills = el('div', { class: 'ds-pillwrap' });
    (s.reports || []).forEach(id => {
      pills.appendChild(el('a', {
        class: 'ds-pill', href: '/d/' + id + '?dock=data',
        text: REPORT_TITLES[id] || id,
      }));
    });
    reach.appendChild(pills);
    if (s.report_count > 1) {
      reach.appendChild(el('div', {
        class: 'ds-note',
        text: 'Changes below apply to this source, so they affect all '
              + s.report_count + ' of these reports.',
      }));
    }
    bodyEl.appendChild(reach);

    if (!isFolder) {
      bodyEl.appendChild(el('div', {
        class: 'ds-note',
        text: 'A GreenPlum source is defined by its frozen SQL in the generator '
              + 'bundle. There is nothing to exclude here — change the queries '
              + 'and publish a new version instead.',
      }));
      drawer.hidden = false; scrim.hidden = false;
      drawer.querySelector('[data-drawer-close]').focus();
      return;
    }

    // — What the application sent. Kept apart from "Identity" below: there a person
    //   DECLARES a path by hand, here the application REPORTS it about its own machine, and
    //   two sources of truth must not be mixed in one field. A browser source
    //   does not get this block at all: it has no path. —
    if (s.root_path || (s.file_paths && s.file_paths.length)) {
      const rep = el('div', { class: 'ds-drawer-sect' });
      rep.appendChild(el('h3', { text: t('Reported by the desktop app') }));
      if (s.root_path) {
        rep.appendChild(el('label', { class: 'ds-lbl', text: t('Folder path') }));
        rep.appendChild(window.OPSourcePaths.pathRow(s.root_path));
        if (s.root_id) {
          rep.appendChild(el('div', { class: 'ds-note', text: t('Path fingerprint (root id): {id}', { id: s.root_id }) }));
        }
      }
      if (s.file_paths && s.file_paths.length) {
        rep.appendChild(el('label', { class: 'ds-lbl', text: t('Files read on the last run') }));
        s.file_paths.forEach(fp => rep.appendChild(window.OPSourcePaths.pathRow(fp)));
        if (s.names_capped) {
          rep.appendChild(el('div', { class: 'ds-note', text: t('The list is capped; the source holds more files.') }));
        }
      }
      rep.appendChild(el('div', {
        class: 'ds-note',
        text: t('This is what the app says about its own machine; the platform does not verify it.'),
      }));
      bodyEl.appendChild(rep);
    }

    // — Identity —
    const ident = el('div', { class: 'ds-drawer-sect' });
    ident.appendChild(el('h3', { text: 'Identity' }));
    ident.appendChild(el('label', { class: 'ds-lbl', text: 'Full path' }));
    const pathIn = el('input', {
      class: 'input sm mono', type: 'text', value: s.declared_path || '',
      placeholder: '\\\\share\\OPS\\RDC\\' + s.name,
    });
    ident.appendChild(pathIn);
    ident.appendChild(el('div', {
      class: 'ds-note',
      text: 'The browser only ever sees the folder’s name, never where it '
            + 'lives. Stating the path here is what tells this source apart from '
            + 'another folder of the same name — nothing else can. (A source '
            + 'read by the desktop app reports its own path above.)',
    }));
    ident.appendChild(el('label', { class: 'ds-lbl', text: 'Label' }));
    const labelIn = el('input', {
      class: 'input sm', type: 'text', value: s.label || '',
      placeholder: 'What this folder is, in words',
    });
    ident.appendChild(labelIn);
    bodyEl.appendChild(ident);

    // — Overrides (Data Lineage v2) — one block per generator that has
    // actually observed this source, fetched from
    // GET /api/data-sources/{id}/overrides. Replaces the old folder-level
    // "Exclude from reading" editor: a junk-file rule is knowledge about
    // what ONE generator can read, not a property of the folder — the same
    // folder read by two generators may need two different rule sets, and
    // the old editor could not tell them apart.
    const ovrSect = el('div', { class: 'ds-drawer-sect' });
    ovrSect.appendChild(el('h3', { text: 'Overrides' }));
    const ovrBody = el('div', {}, [el('div', { class: 'ds-note', text: 'Loading…' })]);
    ovrSect.appendChild(ovrBody);
    bodyEl.appendChild(ovrSect);

    // Defensive: source_id/version only exist on a row once the source has
    // been observed or claimed and the page's payload carries them — an
    // older payload shape degrades to "not available" instead of a crash.
    if (s.source_id == null) {
      ovrBody.textContent = '';
      ovrBody.appendChild(el('div', {
        class: 'ds-note',
        text: 'Overrides are not available for this source yet.',
      }));
    } else {
      fetch('/api/data-sources/' + s.source_id + '/overrides')
        .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(list => renderOverrides(list))
        .catch(() => {
          ovrBody.textContent = '';
          ovrBody.appendChild(el('div', { class: 'ds-note ds-danger', text: 'Could not load overrides.' }));
        });
    }

    function renderOverrides(list) {
      ovrBody.textContent = '';
      if (!list || !list.length) {
        ovrBody.appendChild(el('div', {
          class: 'ds-ovr-empty',
          text: 'No generator you can run has observed this source yet.',
        }));
        return;
      }
      list.forEach(entry => ovrBody.appendChild(renderGenOverride(entry)));
    }

    // One generator's rule editor: its own list, its own live preview against
    // s.file_names, its own Save — the `version` closed over here is THIS
    // generator's input_overrides row, never the source's.
    function renderGenOverride(entry) {
      const block = el('div', { class: 'ds-ovr-gen' });
      let genRules = (entry.exclude_rules || []).slice();
      let version = entry.version;

      block.appendChild(el('div', { class: 'ds-ovr-gen-head' }, [
        el('span', { class: 'ds-ovr-gen-name', text: entry.generator_name }),
        el('span', {
          class: 'ds-ovr-gen-meta mono',
          text: entry.updated_at ? 'edited ' + String(entry.updated_at).slice(0, 10) : 'never edited',
        }),
      ]));

      // Declared inputs for THIS generator against THIS source — one of the
      // page's pre-rendered <template data-sd-source data-sd-gen> blocks
      // (server-side, one per reader per source, same schema_diff.html
      // macro the viewer's Data dock uses) so the drawer never re-implements
      // pattern/role/badge rendering in JS. A generator that reads the
      // source but was not one of build()'s per-source `generator_inputs`
      // keys (payload from an older deploy, or the source has zero declared
      // patterns) has no matching template and is skipped silently.
      const diffTpl = document.querySelector(
        'template[data-sd-template][data-sd-source="' + CSS.escape(s.key) + '"]' +
        '[data-sd-gen="' + CSS.escape(entry.generator_name) + '"]');
      if (diffTpl) {
        block.appendChild(diffTpl.content.cloneNode(true));
      }

      const list = el('div', { class: 'ds-rules' });
      block.appendChild(list);

      const addRow = el('div', { class: 'ds-addrow' });
      const addIn = el('input', { class: 'input sm mono', type: 'text', placeholder: '*_template.xlsx' });
      const addBtn = el('button', { class: 'btn xs', type: 'button', text: 'Add' });
      addRow.appendChild(addIn); addRow.appendChild(addBtn);
      block.appendChild(addRow);

      const preview = el('div', { class: 'ds-preview' });
      block.appendChild(preview);

      const foot = el('div', { class: 'ds-drawer-foot' });
      foot.style.marginTop = '8px'; foot.style.paddingTop = '8px';
      const genStatus = el('span', { class: 'ds-status mono' });
      const genSave = el('button', { class: 'btn xs primary', type: 'button', text: 'Save' });
      foot.appendChild(genStatus); foot.appendChild(genSave);
      block.appendChild(foot);

      function setGenStatus(msg, kind) {
        genStatus.textContent = msg || '';
        genStatus.style.color = kind === 'err' ? 'var(--danger)'
          : kind === 'ok' ? 'var(--success)' : 'var(--ink-dim)';
      }

      // Same refusal, same wording, as globmatch.validate_pattern on the
      // server — the walk is one level deep, so a pattern that could only
      // ever match inside a subfolder would be accepted here and silently
      // never fire.
      function addRule() {
        const v = addIn.value.trim();
        if (!v || genRules.includes(v)) { addIn.value = ''; return; }
        if (v.includes('/') || v.includes('\\')) {
          setGenStatus("pattern must not contain '/' or '\\' (folders are read one level deep)", 'err');
          return;
        }
        if (v.replace(/\*/g, '').replace(/\?/g, '') === '') {
          setGenStatus('A pattern of only wildcards would exclude every file.', 'err');
          return;
        }
        genRules.push(v); addIn.value = ''; renderRuleList();
      }
      addBtn.addEventListener('click', addRule);
      addIn.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); addRule(); }
      });
      addIn.addEventListener('input', renderRulePreview);

      function renderRuleList() {
        list.textContent = '';
        if (!genRules.length) {
          list.appendChild(el('div', { class: 'ds-note', text: 'Nothing excluded for this generator.' }));
        }
        genRules.forEach((p, i) => {
          const row = el('div', { class: 'ds-rule' });
          row.appendChild(el('code', { class: 'mono', text: p }));
          const n = (s.file_names || []).filter(f => globToRe(p).test(f)).length;
          row.appendChild(el('span', {
            class: 'ds-rule-n mono' + (n ? '' : ' is-idle'),
            text: n ? n + ' match' + (n === 1 ? '' : 'es') : 'no match',
          }));
          const x = el('button', { class: 'btn xs ghost', type: 'button', text: '✕', title: 'Remove' });
          x.addEventListener('click', () => { genRules.splice(i, 1); renderRuleList(); });
          row.appendChild(x);
          list.appendChild(row);
        });
        renderRulePreview();
      }

      function renderRulePreview() {
        preview.textContent = '';
        const names = s.file_names || [];
        if (!names.length) {
          preview.appendChild(el('div', { class: 'ds-note', text: 'No file list recorded yet — run the report once to see what a rule would drop.' }));
          return;
        }
        const draft = genRules.slice();
        const typing = addIn.value.trim();
        if (typing && typing.replace(/\*/g, '').replace(/\?/g, '') !== '') draft.push(typing);

        const dropped = [], kept = [];
        names.forEach(f => (matchedBy(draft, f) ? dropped : kept).push(f));
        preview.appendChild(el('div', { class: 'ds-preview-head mono' }, [
          el('b', { text: String(kept.length) }), ' kept · ',
          el('b', { class: dropped.length ? 'ds-warn' : '', text: String(dropped.length) }), ' excluded',
          s.names_capped ? ' (of a ' + names.length + '-file sample)' : '',
        ]));
        if (dropped.length) {
          const ul = el('div', { class: 'ds-preview-list mono' });
          dropped.slice(0, 60).forEach(f => ul.appendChild(el('div', { class: 'ds-dropped', text: f })));
          if (dropped.length > 60) ul.appendChild(el('div', { class: 'ds-note', text: '…and ' + (dropped.length - 60) + ' more' }));
          preview.appendChild(ul);
        }
        if (!kept.length && names.length) {
          preview.appendChild(el('div', { class: 'ds-note ds-danger', text: 'These rules would leave nothing to read.' }));
        }
      }
      renderRuleList();

      genSave.addEventListener('click', async () => {
        genSave.disabled = true; setGenStatus('Saving…');
        let res;
        try {
          res = await fetch('/api/data-sources/' + s.source_id + '/overrides/' + entry.generator_id, {
            method: 'PUT', headers: CSRF_SAFE,
            body: JSON.stringify({ exclude_rules: genRules, version: version }),
          });
        } catch (e) {
          genSave.disabled = false; setGenStatus('Could not reach the server.', 'err'); return;
        }
        if (res.status === 409) {
          let detail = null;
          try { detail = (await res.json()).detail; } catch (e) { /* keep null */ }
          const current = detail && detail.current;
          genSave.disabled = false;
          setGenStatus('changed by someone else — reloaded', 'err');
          if (current) {
            genRules = (current.exclude_rules || []).slice();
            version = current.version;
            renderRuleList();
          }
          return;
        }
        if (!res.ok) {
          let d = '';
          try { d = (await res.json()).detail || ''; } catch (e) { /* keep '' */ }
          genSave.disabled = false;
          setGenStatus((typeof d === 'string' && d) || ('Save failed (' + res.status + ')'), 'err');
          return;
        }
        const saved = await res.json();
        genRules = saved.exclude_rules || [];
        version = saved.version;
        genSave.disabled = false;
        setGenStatus('Saved. Takes effect on the next run.', 'ok');
        renderRuleList();
        document.dispatchEvent(new CustomEvent('ds:saved', { detail: { key: key, source: s } }));
      });

      return block;
    }

    // — Annotations (P3.6): a human note per declared sheet/column, saved as
    // one flat form for the whole source (the API replaces the full set on
    // every PUT — see app/api/generators.py:put_annotations). Built entirely
    // from data the page already shipped (s.generator_inputs,
    // s.generator_column_descriptions, s.annotations) — no fetch needed.
    const annSect = el('div', { class: 'ds-drawer-sect' });
    annSect.appendChild(el('h3', { text: 'Annotations' }));
    const targets = annotationTargets(s);
    if (!targets.length) {
      annSect.appendChild(el('div', {
        class: 'ds-note',
        text: 'No declared sheets or columns to annotate yet — a generator with '
              + '`inputs` must read this source first.',
      }));
    } else {
      const existing = new Map();
      (s.annotations || []).forEach(a => {
        existing.set(a.match + '|' + a.sheet + '|' + (a.column || ''), a);
      });
      const fields = [];   // {input, match, sheet, column}
      // Keyed by match|sheet (not "compare to the previous target") so two
      // generators that declare the same match/sheet non-adjacently in
      // `targets` still land in ONE group, in DOM order of first appearance,
      // instead of the sheet being rendered as two separate group cards.
      const groupsByKey = new Map();
      targets.forEach(tv => {
        const groupKey = tv.match + '|' + tv.sheet;
        let group = groupsByKey.get(groupKey);
        if (!group) {
          group = el('div', { class: 'ds-ann-group' });
          group.appendChild(el('div', { class: 'ds-ann-head mono', text: tv.match + ' / ' + tv.sheet }));
          annSect.appendChild(group);
          groupsByKey.set(groupKey, group);
        }
        const key = tv.match + '|' + tv.sheet + '|' + (tv.column || '');
        const cur = existing.get(key);
        const row = el('div', { class: 'ds-ann-row' });
        row.appendChild(el('span', { class: 'ds-ann-lbl mono', title: tv.column || '(sheet)',
                                     text: tv.column || '(sheet)' }));
        const input = el('input', {
          class: 'input sm', type: 'text',
          value: (cur && cur.description) || '',
          placeholder: tv.hint || (tv.column ? 'What this column is' : 'What this sheet is'),
        });
        row.appendChild(input);
        group.appendChild(row);
        fields.push({ input: input, match: tv.match, sheet: tv.sheet, column: tv.column });
      });

      const annFoot = el('div', { class: 'ds-drawer-foot' });
      annFoot.style.marginTop = '8px'; annFoot.style.paddingTop = '8px';
      const annStatus = el('span', { class: 'ds-status mono' });
      const annSave = el('button', { class: 'btn xs primary', type: 'button', text: 'Save annotations' });
      annFoot.appendChild(annStatus); annFoot.appendChild(annSave);
      annSect.appendChild(annFoot);

      function setAnnStatus(msg, kind) {
        annStatus.textContent = msg || '';
        annStatus.style.color = kind === 'err' ? 'var(--danger)'
          : kind === 'ok' ? 'var(--success)' : 'var(--ink-dim)';
      }

      annSave.addEventListener('click', async () => {
        if (s.source_id == null) { setAnnStatus('Not available for this source yet.', 'err'); return; }
        // Blank field = no annotation: the API's delete-missing semantics
        // means an emptied field removes a previously saved note — but only
        // within `known_keys`, the rows this drawer actually rendered. A
        // saved annotation this drawer never showed (a column a newer
        // manifest dropped, a target from a generator this user cannot
        // reach) is out of scope and must survive this save untouched.
        const items = [];
        fields.forEach(f => {
          const desc = f.input.value.trim();
          if (desc) items.push({ match: f.match, sheet: f.sheet, column: f.column, description: desc, tags: [] });
        });
        const knownKeys = fields.map(f => ({ match: f.match, sheet: f.sheet, column: f.column }));
        annSave.disabled = true; setAnnStatus('Saving…');
        let res;
        try {
          res = await fetch('/api/data-sources/' + s.source_id + '/annotations', {
            method: 'PUT', headers: CSRF_SAFE,
            body: JSON.stringify({ items: items, known_keys: knownKeys }),
          });
        } catch (e) {
          annSave.disabled = false; setAnnStatus('Could not reach the server.', 'err'); return;
        }
        if (!res.ok) {
          let d = '';
          try { d = (await res.json()).detail || ''; } catch (e) { /* keep '' */ }
          annSave.disabled = false;
          setAnnStatus((typeof d === 'string' && d) || ('Save failed (' + res.status + ')'), 'err');
          return;
        }
        const saved = await res.json();
        s.annotations = saved;
        annSave.disabled = false;
        setAnnStatus('Saved.', 'ok');
      });
    }
    bodyEl.appendChild(annSect);

    // — Save —
    const foot = el('div', { class: 'ds-drawer-foot' });
    const status = el('span', { class: 'ds-status mono' });
    const save = el('button', { class: 'btn sm primary', type: 'button', text: 'Save' });
    foot.appendChild(status); foot.appendChild(save);
    bodyEl.appendChild(foot);

    function setStatus(msg, kind) {
      status.textContent = msg || '';
      status.style.color = kind === 'err' ? 'var(--danger)'
        : kind === 'ok' ? 'var(--success)' : 'var(--ink-dim)';
    }

    save.addEventListener('click', async () => {
      save.disabled = true; setStatus('Saving…');
      let res;
      try {
        res = await fetch('/api/data-sources', {
          method: 'PUT', headers: CSRF_SAFE,
          body: JSON.stringify({
            kind: s.kind, key_name: s.name,
            declared_path: pathIn.value, label: labelIn.value,
            // `s.version` is undefined on a payload from a build that has not
            // added it yet — JSON.stringify drops an undefined key, and the
            // server treats a missing version as "skip the conflict check",
            // so this degrades to the old unconditional-overwrite behaviour
            // rather than breaking the save.
            version: s.version,
          }),
        });
      } catch (e) {
        save.disabled = false; setStatus('Could not reach the server.', 'err'); return;
      }
      if (res.status === 409) {
        let detail = null;
        try { detail = (await res.json()).detail; } catch (e) { /* keep null */ }
        const current = detail && detail.current;
        save.disabled = false;
        setStatus('changed by someone else — reloaded', 'err');
        if (current) {
          s.declared_path = current.declared_path;
          s.label = current.label;
          s.notes = current.notes;
          s.version = current.version;
          pathIn.value = s.declared_path || '';
          labelIn.value = s.label || '';
        }
        return;
      }
      if (!res.ok) {
        let d = '';
        try { d = (await res.json()).detail || ''; } catch (e) { /* keep '' */ }
        save.disabled = false;
        setStatus((typeof d === 'string' && d) || ('Save failed (' + res.status + ')'), 'err');
        return;
      }
      const saved = await res.json();
      s.declared_path = saved.declared_path;
      s.label = saved.label;
      s.version = saved.version;
      save.disabled = false;
      setStatus('Saved. Takes effect on the next run.', 'ok');
      // The row's "by name" chip and the canvas ring both derive from this.
      document.dispatchEvent(new CustomEvent('ds:saved', { detail: { key: key, source: s } }));
      applyFilter();
    });

    drawer.hidden = false; scrim.hidden = false;
    pathIn.focus();
  }

  document.addEventListener('ds:open', e => open(e.detail.key, e.detail.trigger));
  document.querySelectorAll('[data-src-edit]').forEach(btn => {
    btn.addEventListener('click', ev => {
      ev.preventDefault(); ev.stopPropagation();
      open(btn.dataset.srcEdit, btn);
    });
  });

  // A saved claim removes the "by name" chip without a reload.
  document.addEventListener('ds:saved', e => {
    const tr = document.querySelector('[data-src-row="' + CSS.escape(e.detail.key) + '"]');
    if (!tr) return;
    const chip = tr.querySelector('.ds-chip');
    if (chip && e.detail.source.declared_path) chip.remove();
  });

  applyFilter();
})();
