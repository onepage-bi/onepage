/* Data Sources — column-level lineage graph.
 *
 * Six layers, left to right: folder -> file pattern -> sheet (+columns) ->
 * generator -> output -> report. The layers in the middle are the whole point:
 * a folder does not feed a report, a pattern inside it matches files, a sheet
 * inside those files carries columns, and a generator reads them. Anything
 * that collapses that away is a list of pairs, not a lineage.
 *
 * Cards are real DOM, absolutely positioned; only the edges are SVG on a layer
 * behind them. foreignObject would have kept one tree but brings its own layout
 * and font bugs, and the cards need ordinary CSS far more than they need to
 * live inside the <svg>.
 *
 * Positions come from dagre (vendored, rankdir LR) rather than the hand-rolled
 * barycentre sweep this file replaces: six layers with collapsible middles need
 * dummy nodes on long edges, which is most of dagre already.
 *
 * Sizes are MEASURED from the DOM before layout, not assumed: a sheet card
 * carrying twenty column chips is four times the height of a folder card, and
 * feeding dagre a constant would overlap them.
 */
(function () {
  'use strict';

  const section = document.getElementById('lineage');
  if (!section) return;

  const viewport = section.querySelector('[data-lin-viewport]');
  const world = section.querySelector('[data-lin-world]');
  const cardsEl = section.querySelector('[data-lin-cards]');
  const edgeSvg = section.querySelector('[data-lin-edges]');
  const skeleton = section.querySelector('[data-lin-skeleton]');
  const errorEl = section.querySelector('[data-lin-error]');
  const zoomLbl = section.querySelector('[data-lin-zoom-pct]');
  const unfocusBtn = section.querySelector('[data-lin-act="unfocus"]');
  const detailSel = section.querySelector('[data-lin-detail]');
  const exportSel = section.querySelector('[data-lin-export]');
  if (!viewport || !world || !cardsEl || !edgeSvg) return;

  const NS = 'http://www.w3.org/2000/svg';
  const DEV = section.dataset.lineageDev === '1';
  const K_MIN = 0.25, K_MAX = 3;
  const LOOP_GAP = 30;          // vertical clearance for a writes_back loop
  const MAX_CHIPS = 12;         // a sheet card is a summary, not a schema dump

  let NODES = [];
  let EDGES = [];
  let byId = new Map();
  let bySrcKey = new Map();     // "folder:Name" -> node, the drawer's key space
  let level = 'sheets';
  const collapsed = new Set();  // pattern ids whose sheets are folded away
  let sticky = null;            // node focused by a click, survives hover
  let filterKeys = null;        // from ds:filter; null means "no filter"
  let view = { x: 16, y: 16, k: 1 };
  let bounds = { w: 0, h: 0 };
  let rendered = [];            // visible nodes of the last render
  let drawn = [];               // {el, a, b, edge} of the last render
  // source_id -> [{match, sheet, column, description}], reloaded from
  // #ds-root on every build() — see "-- Annotations --" below.
  let annBySource = new Map();

  // -- Data ----------------------------------------------------------
  function build(data) {
    NODES = (data.nodes || []).map(function (n) {
      return Object.assign({}, n, { out: [], in: [], w: 0, h: 0, x: 0, y: 0, visible: false });
    });
    byId = new Map(NODES.map(n => [n.id, n]));
    bySrcKey = new Map();
    NODES.forEach(n => { if (n.layer === 0) bySrcKey.set(n.kind + ':' + n.label, n); });
    EDGES = [];
    (data.edges || []).forEach(e => {
      const a = byId.get(e.from), b = byId.get(e.to);
      if (!a || !b) return;                   // an edge to nowhere is not drawn
      const edge = { a: a, b: b, kind: e.kind };
      EDGES.push(edge);
      a.out.push(edge); b.in.push(edge);
    });
    // A sheet belongs to exactly one pattern; that link is what collapse folds.
    NODES.forEach(n => {
      if (n.kind !== 'sheet') return;
      const p = n.in.find(e => e.a.kind === 'pattern');
      n.parent = p ? p.a.id : null;
    });
    // A stage belongs to exactly one generator (P4.1) — carried on the node's
    // own data rather than found by walking edges, because a stage's only
    // edges are to OTHER stages/files, never straight to "its" generator
    // unless it happens to be a terminal one.
    NODES.forEach(n => {
      if (n.kind !== 'stage') return;
      // A stage with no generator name (a hand-built/fixture graph, or a
      // future server change that stops echoing it) has nothing to fold
      // under: leaving parent null shows it, unattached, instead of filing
      // it under the bogus 'gen:' id and folding it away with no twisty
      // anywhere that could ever reveal it again.
      const gname = (n.data || {}).generator;
      n.parent = gname ? 'gen:' + gname : null;
    });

    collapsed.clear();
    const patterns = NODES.filter(n => n.kind === 'pattern');
    // Twelve patterns is where the sheet layer stops fitting a laptop screen at
    // 100%; above it the graph opens folded and the reader expands what they
    // came for.
    if (patterns.length > 12) patterns.forEach(p => collapsed.add(p.id));
    // A generator's stage chain starts folded: it is a drill-down into HOW the
    // generator got from files to output, not something every reader needs
    // open to read the rest of the graph.
    const gensWithStages = new Set(NODES.filter(n => n.kind === 'stage' && n.parent).map(n => n.parent));
    gensWithStages.forEach(id => collapsed.add(id));

    loadAnnotations();
  }

  // -- Annotations (P4.2) ----------------------------------------------
  // A sheet/column "description" comes from a data-owner Annotation row,
  // scoped by data_source_id. But a sheet or pattern NODE in this graph is
  // keyed by (generator, match, sheet[, column]) alone — the same key a
  // second folder feeding the same declared pattern would also match. So the
  // graph's own baked description is not trusted here: it is recomputed from
  // the per-source payload the drawer already uses (#ds-root's data-sources,
  // already scoped by source_id server-side — app/api/generators.py's
  // `sources_json`), restricted to the source(s) this particular node is
  // actually fed by.
  function loadAnnotations() {
    annBySource = new Map();
    const root = document.getElementById('ds-root');
    if (!root) return;
    let sources = [];
    try { sources = JSON.parse(root.dataset.sources || '[]'); } catch (e) { return; }
    sources.forEach(s => {
      if (s && s.source_id != null) annBySource.set(s.source_id, s.annotations || []);
    });
  }

  // The source(s) actually feeding a pattern node, read off the 'matches'
  // edges drawn in build() — a pattern has no source_id of its own.
  function sourceIdsOfPattern(pat) {
    if (!pat) return [];
    return pat.in
      .filter(e => e.a.layer === 0)
      .map(e => (e.a.data || {}).source_id)
      .filter(id => id !== undefined && id !== null);
  }

  function annotationLookup(sourceIds, match, sheet, column) {
    if (!sourceIds.length) return null;
    const sheets = sheet === '*' ? ['*'] : [sheet, '*'];
    for (const sid of sourceIds) {
      const list = annBySource.get(sid);
      if (!list || !list.length) continue;
      for (const sh of sheets) {
        const hit = list.find(a => a.match === match && a.sheet === sh
          && (a.column || null) === (column || null));
        if (hit) return hit.description;
      }
    }
    return null;
  }

  // A sheet card's own description (column=null). Scoped to the source(s)
  // that feed its pattern when that is knowable; otherwise falls back to
  // whatever the graph baked in (dev fixture, greenplum reads, no #ds-root
  // on the page) rather than showing nothing.
  function sheetDescription(n) {
    const pat = n.parent ? byId.get(n.parent) : null;
    const ids = sourceIdsOfPattern(pat);
    if (pat && ids.length) {
      const hit = annotationLookup(ids, pat.label, n.label, null);
      if (hit != null) return hit;
    }
    // No per-source annotation for this node (or no source to scope to): fall
    // back to whatever the graph already baked in (manifest P3.6 description,
    // dev fixture, greenplum reads) rather than showing nothing.
    return (n.data || {}).description || null;
  }

  function columnDescription(n, col) {
    const pat = n.parent ? byId.get(n.parent) : null;
    const ids = sourceIdsOfPattern(pat);
    if (pat && ids.length) {
      const hit = annotationLookup(ids, pat.label, n.label, col.name);
      if (hit != null) return hit;
    }
    return col.description || null;
  }

  // -- Visibility and edge contraction --------------------------------
  function isVisible(n) {
    if (n.kind === 'pattern') return level !== 'folders';
    if (n.kind === 'sheet') return level === 'sheets' && !collapsed.has(n.parent);
    if (n.kind === 'stage') return level === 'sheets' && !collapsed.has(n.parent);
    return true;
  }

  /* Edges of the folded graph. A hidden node is not dropped, it is CONTRACTED:
     every visible node upstream of it gains an edge to every visible node
     downstream, so folding the sheet layer still shows that this folder feeds
     that generator. Chains of hidden nodes are walked transitively. */
  function contract() {
    const out = [];
    const seenPair = new Set();
    function emit(a, b, kind, via) {
      if (a === b) return;
      const k = a.id + ' ' + b.id;
      if (seenPair.has(k)) return;
      seenPair.add(k);
      out.push({ a: a, b: b, kind: kind, via: via });
    }
    NODES.forEach(a => {
      if (!a.visible) return;
      a.out.forEach(e0 => {
        if (e0.b.visible) { emit(a, e0.b, e0.kind, false); return; }
        const seen = new Set();
        const stack = [e0.b];
        while (stack.length) {
          const h = stack.pop();
          if (seen.has(h.id)) continue;
          seen.add(h.id);
          h.out.forEach(e => {
            if (e.b.visible) emit(a, e.b, e0.kind, true);
            else stack.push(e.b);
          });
        }
      });
    });
    return out;
  }

  // -- Card rendering -------------------------------------------------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function icon(href, cls) {
    const s = document.createElementNS(NS, 'svg');
    s.setAttribute('class', 'ico ico-12 ' + (cls || ''));
    const u = document.createElementNS(NS, 'use');
    u.setAttribute('href', href);
    s.appendChild(u);
    return s;
  }
  function fmtBytes(b) {
    if (!b) return null;
    const mb = b / 1048576;
    return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(mb < 10 ? 1 : 0) + ' MB';
  }
  function fmtAge(d) {
    if (d === null || d === undefined) return null;
    if (d === 0) return 'today';
    if (d === 1) return 'yesterday';
    return d < 30 ? d + 'd ago' : Math.round(d / 30) + 'mo ago';
  }
  const ICONS = {
    folder: '#ic-folder', greenplum: '#ic-database', pattern: '#ic-file',
    sheet: '#ic-rows', stage: '#ic-scan', generator: '#ic-zap', output: '#ic-file',
    write_back: '#ic-upload', report: '#ic-grid',
  };

  function metaOf(n) {
    const d = n.data || {};
    const bits = [];
    if (n.kind === 'folder') {
      if (d.files) bits.push(d.files + (d.files === 1 ? ' file' : ' files'));
      const b = fmtBytes(d.bytes); if (b) bits.push(b);
      const a = fmtAge(d.data_age_days); if (a) bits.push(a);
    } else if (n.kind === 'greenplum') {
      const q = (d.queries || []).length;
      if (q) bits.push(q + (q === 1 ? ' query' : ' queries'));
    } else if (n.kind === 'pattern') {
      if (d.role) bits.push(d.role);
      if (d.matched_files !== undefined && d.matched_files !== null) {
        bits.push(d.matched_files + (d.matched_files === 1 ? ' file' : ' files'));
      }
    } else if (n.kind === 'sheet') {
      const c = (d.columns || []).length;
      if (c) bits.push(c + (c === 1 ? ' column' : ' columns'));
      if (d.observed_rows) bits.push(tn(d.observed_rows, '{p1} row|{p1} rows', {p1: OP_I18N.fmtNum(d.observed_rows)}));
    } else if (n.kind === 'stage') {
      if (d.rows !== null && d.rows !== undefined) bits.push(tn(d.rows, '{p1} row|{p1} rows', {p1: OP_I18N.fmtNum(d.rows)}));
      const c = (d.columns || []).length;
      if (c) bits.push(c + (c === 1 ? ' column' : ' columns'));
      if (d.ms !== null && d.ms !== undefined) bits.push(d.ms + ' ms');
    } else if (n.kind === 'generator') {
      if (d.version) bits.push('v' + d.version);
      if (d.engine) bits.push(d.engine);
    } else if (n.kind === 'output') {
      bits.push(d.slot === null || d.slot === undefined ? 'html' : 'slot ' + d.slot);
    } else if (n.kind === 'write_back') {
      if ((d.targets || []).length) bits.push('to ' + d.targets.join(', '));
      if (d.last_written) bits.push(d.last_written);
    } else if (n.kind === 'report') {
      if (d.engine) bits.push(d.engine);
      if (d.last_run_at) bits.push(d.last_run_at);
    }
    return bits.join(' · ');
  }

  function truncate(s, max) {
    s = String(s || '');
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  }

  function chips(n, head) {
    const d = n.data || {};
    function chip(text, cls, title) {
      const c = el('span', 'lin-chip ' + (cls || ''), text);
      if (title) c.title = title;
      head.appendChild(c);
    }
    if (n.kind === 'folder') {
      if (d.shared) chip('shared', 'is-accent', 'Read by more than one report');
      if (!d.declared_path && !d.root_id) chip('by name', 'is-warn', 'No path claimed - identity is the folder name only');
      else if (!d.declared_path) chip(t('path reported'), 'is-muted', t('The desktop app reported this folder\'s path; the platform does not verify it'));
      if (d.unread) chip('unread', 'is-muted', 'No recent run has read this source');
    } else if (n.kind === 'pattern') {
      if (d.required) chip('required', 'is-warn');
      if (d.legacy) chip('legacy', 'is-muted', 'No inputs declared - the generator reads every Excel file');
      if (d.unmatched) chip('no match', 'is-danger', 'This pattern matched no file on the last run');
      // schema_errors (commit 633eaaa's per-file top-level `error`, tallied by
      // build_graph onto the pattern) - a workbook that would not open at all,
      // not a per-sheet header miss (that chip lives on the sheet card).
      Object.keys(d.schema_errors || {}).forEach(function (errText) {
        const nFiles = d.schema_errors[errText];
        chip(tn(nFiles, '{n} file without a schema: {p3}|{n} files without a schema: {p3}',
          {p3: truncate(errText, 40)}),
          'is-muted', t('{p1} file(s) without schema: {p2}', {p1: nFiles, p2: errText}));
      });
    } else if (n.kind === 'sheet') {
      // header_missing (commit 633eaaa's per-sheet `reason`) - the sheet node
      // exists (a pattern/generator declared or observed it) but no run ever
      // located a header row for it; the reason is the majority text across
      // the files build_graph looked at (see _observed_sheets/_majority_reason).
      if (d.header_missing) chip(t('header not found'), 'is-warn', d.reason || t('reason unknown'));
    } else if (n.kind === 'generator') {
      if (d.observed === false) chip('declared', 'is-muted', 'Never observed reading - agent engine');
      if (d.legacy_inputs) chip('legacy', 'is-muted');
      // Stages (P4.1): a chain the generator reported via onepage.stage() -
      // the chip is the constant "this exists, N steps" cue; the twisty on
      // the card head is what actually opens the chain.
      if (d.stages) {
        chip(d.stages + (d.stages === 1 ? ' stage' : ' stages'), 'is-muted',
          'Steps this generator ran, in order - click the arrow to expand');
      }
    } else if (n.kind === 'write_back') {
      if ((d.failed || []).length) chip(d.failed.length + ' failed', 'is-danger', d.failed.join(', '));
    }
  }

  function columnList(n) {
    const cols = (n.data || {}).columns || [];
    if (!cols.length) return null;
    const wrap = el('div', 'lin-cols');
    cols.slice(0, MAX_CHIPS).forEach(c => {
      // Three states, three meanings: declared and seen (they agree), declared
      // and never seen (the generator will KeyError), seen but never declared
      // (the manifest is behind the workbook).
      const state = c.declared && c.observed ? 'ok' : c.declared ? 'warn' : 'muted';
      const chip = el('span', 'lin-col is-' + state);
      chip.appendChild(el('i', 'dot'));
      chip.appendChild(el('span', 'nm', c.name));
      if (c.consumed === true) chip.appendChild(icon('#ic-check', 'ico-10 tick'));
      // The description (P3.6/P4.2) is resolved client-side, scoped to the
      // source(s) this sheet is actually fed by — see columnDescription();
      // it comes first so the tooltip leads with "what this is", not the
      // tri-state plumbing.
      const desc = columnDescription(n, c);
      chip.title = (desc ? desc + '\n' : '') + c.name + ' - '
        + (state === 'ok' ? 'declared and observed'
          : state === 'warn' ? 'declared but not observed in the workbook'
            : 'observed in the workbook but not declared')
        + (c.consumed === true ? ', read by the generator' : '');
      wrap.appendChild(chip);
    });
    if (cols.length > MAX_CHIPS) {
      wrap.appendChild(el('span', 'lin-col is-more', '+' + (cols.length - MAX_CHIPS) + ' more'));
    }
    return wrap;
  }

  // A stage's columns (P4.1) are a plain name list from the contract, not the
  // sheet's tri-state declared/observed/consumed shape — there is nothing to
  // diff against, so the chip carries no dot and no state class.
  function stageColumns(n) {
    const cols = (n.data || {}).columns || [];
    if (!cols.length) return null;
    const wrap = el('div', 'lin-cols');
    cols.slice(0, MAX_CHIPS).forEach(name => {
      const chip = el('span', 'lin-col is-plain');
      chip.appendChild(el('span', 'nm', name));
      chip.title = name;
      wrap.appendChild(chip);
    });
    if (cols.length > MAX_CHIPS) {
      wrap.appendChild(el('span', 'lin-col is-more', '+' + (cols.length - MAX_CHIPS) + ' more'));
    }
    return wrap;
  }

  function paint(n, card) {
    card.textContent = '';
    card.className = 'lin-card k-' + n.kind;
    const d = n.data || {};
    if (d.data_stale) card.classList.add('is-stale');
    if (d.unread) card.classList.add('is-unread');
    if (d.shared) card.classList.add('is-shared');
    if (d.unmatched || (d.failed || []).length) card.classList.add('is-failing');
    if (n.kind === 'report' && d.status === 'error') card.classList.add('is-failing');

    const head = el('div', 'lin-card-head');
    // Two things fold behind a twisty: a pattern's sheets, and (P4.1) a
    // generator's stage chain — same affordance, different word in the
    // fold-summary line below.
    const foldWord = n.kind === 'generator' ? 'stage' : 'sheet';
    const foldCount = n.kind === 'pattern' ? n.out.filter(e => e.b.kind === 'sheet').length
      : n.kind === 'generator' ? (d.stages || 0) : 0;
    const foldable = foldCount > 0 && level === 'sheets';
    if (foldable) {
      const tw = el('button', 'lin-twisty');
      tw.type = 'button';
      tw.setAttribute('aria-expanded', String(!collapsed.has(n.id)));
      tw.title = collapsed.has(n.id) ? ('Show ' + foldWord + 's') : ('Hide ' + foldWord + 's');
      tw.appendChild(icon(collapsed.has(n.id) ? '#ic-chevron-right' : '#ic-chevron-down', 'ico-10'));
      tw.addEventListener('click', ev => { ev.stopPropagation(); toggle(n); });
      head.appendChild(tw);
    }
    head.appendChild(icon(ICONS[n.kind] || '#ic-file', 'lin-card-ico'));
    const tv = el('span', 'lin-card-title', n.label);
    // A sheet-level annotation ("what this sheet is", column=null), resolved
    // client-side and scoped to this sheet's own source(s) - see
    // sheetDescription() (P4.2). Folders get their declared path instead,
    // sheets have no path of their own, so the two never compete for the
    // same line.
    const desc = n.kind === 'sheet' ? sheetDescription(n) : d.description;
    tv.title = n.label + (d.declared_path || d.root_path ? '\n' + (d.declared_path || d.root_path) : '')
      + (desc ? '\n' + desc : '');
    head.appendChild(tv);
    chips(n, head);
    card.appendChild(head);

    const m = metaOf(n);
    if (m) card.appendChild(el('div', 'lin-card-meta mono', m));
    if (n.kind === 'folder' && (d.declared_path || d.root_path)) {
      // A path declared by a person outranks one sent by the application: it is edited
      // right here, while the sent one is overwritten by every run.
      card.appendChild(el('div', 'lin-card-path mono', d.declared_path || d.root_path));
    }
    const cols = n.kind === 'sheet' ? columnList(n) : n.kind === 'stage' ? stageColumns(n) : null;
    if (cols) card.appendChild(cols);
    if (foldable && collapsed.has(n.id)) {
      card.appendChild(el('div', 'lin-card-meta mono is-fold',
        foldCount + ' ' + foldWord + (foldCount === 1 ? '' : 's') + ' folded'));
    }
  }

  const cardOf = new Map();
  function cardFor(n) {
    let card = cardOf.get(n.id);
    if (!card) {
      const dash = (n.data || {}).dashboard_id;
      card = n.kind === 'report' && dash ? document.createElement('a') : document.createElement('div');
      if (card.tagName === 'A') card.href = '/d/' + dash + '?dock=data';
      card.tabIndex = 0;
      card.dataset.node = n.id;
      cardOf.set(n.id, card);
      cardsEl.appendChild(card);
      wire(n, card);
    }
    paint(n, card);
    return card;
  }

  // -- Interaction ----------------------------------------------------
  function wire(n, card) {
    card.addEventListener('click', ev => {
      if (dragged) { ev.preventDefault(); return; }
      // A folder card is the table row's gear by another route: same key space,
      // same drawer.
      if (n.layer === 0) {
        document.dispatchEvent(new CustomEvent('ds:open', {
          detail: { key: n.kind + ':' + n.label, trigger: card },
        }));
      }
      setSticky(sticky === n.id ? null : n.id);
    });
    card.addEventListener('mouseenter', () => { if (!sticky) highlight(n); });
    card.addEventListener('mouseleave', () => { if (!sticky) highlight(null); });
    card.addEventListener('keydown', ev => {
      const foldable = n.kind === 'pattern' || (n.kind === 'generator' && (n.data || {}).stages);
      if (ev.key === 'Enter' && foldable) { ev.preventDefault(); toggle(n); }
      else if (ev.key === 'Escape') setSticky(null);
    });
    card.addEventListener('focus', () => { if (!sticky) highlight(n); });
    card.addEventListener('blur', () => { if (!sticky) highlight(null); });
  }

  function toggle(n) {
    if (collapsed.has(n.id)) collapsed.delete(n.id); else collapsed.add(n.id);
    render();
    const card = cardOf.get(n.id);
    if (card) card.focus({ preventScroll: true });
  }

  function reach(n) {
    const live = new Set([n.id]);
    const up = [n], down = [n];
    while (up.length) {
      const x = up.pop();
      x.in.forEach(e => { if (!live.has(e.a.id)) { live.add(e.a.id); up.push(e.a); } });
    }
    while (down.length) {
      const x = down.pop();
      x.out.forEach(e => { if (!live.has(e.b.id)) { live.add(e.b.id); down.push(e.b); } });
    }
    return live;
  }

  /* Both directions on purpose: from a folder the question is "which reports
     would a change here move", from a report "where does this actually come
     from". One gesture, both answers. */
  function highlight(n) {
    if (!n) {
      section.classList.remove('is-focus');
      cardOf.forEach(c => c.classList.remove('hot'));
      drawn.forEach(d => d.el.classList.remove('hot'));
      return;
    }
    const live = reach(n);
    section.classList.add('is-focus');
    cardOf.forEach((c, id) => c.classList.toggle('hot', live.has(id)));
    drawn.forEach(d => d.el.classList.toggle('hot', live.has(d.a.id) && live.has(d.b.id)));
  }

  function setSticky(id) {
    sticky = id;
    highlight(id ? byId.get(id) : null);
    // The toolbar's only way to clear a focus that isn't near the mouse
    // (typed a deep link, tabbed to a card) - so it has to track sticky, not
    // just sit there.
    if (unfocusBtn) unfocusBtn.hidden = !id;
    // The focus is the interesting state of this page, so it belongs in the URL
    // - a colleague gets the same trace, not the same picture. replaceState, not
    // push: focusing card after card must not fill the back button with a
    // history of glances.
    const u = new URL(location.href);
    if (id) u.searchParams.set('focus', id);
    else if (u.searchParams.has('focus')) u.searchParams.delete('focus');
    else return;
    history.replaceState(null, '', u);
  }

  // -- Layout ---------------------------------------------------------
  function render() {
    if (!NODES.length) return;
    const t0 = performance.now();
    NODES.forEach(n => { n.visible = isVisible(n); });
    rendered = NODES.filter(n => n.visible);

    cardOf.forEach((card, id) => {
      const n = byId.get(id);
      card.style.display = n && n.visible ? '' : 'none';
    });
    rendered.forEach(n => { cardFor(n).style.display = ''; });
    // Written first, read after: one forced reflow for the whole batch.
    // offsetWidth is immune to the wrapper's scale, getBoundingClientRect is not.
    rendered.forEach(n => {
      const card = cardOf.get(n.id);
      n.w = card.offsetWidth;
      n.h = card.offsetHeight;
    });

    const edges = contract();
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: 'LR', nodesep: 12, ranksep: 72, marginx: 16, marginy: 16 });
    g.setDefaultEdgeLabel(() => ({}));
    rendered.forEach(n => g.setNode(n.id, { width: n.w, height: n.h }));
    // writes_back runs back up the graph; handing it to dagre would either make
    // a cycle or drag the folder into the last rank. It is drawn by hand below.
    edges.forEach(e => { if (e.kind !== 'writes_back') g.setEdge(e.a.id, e.b.id); });
    dagre.layout(g);

    rendered.forEach(n => {
      const p = g.node(n.id);
      n.x = p.x - n.w / 2;
      n.y = p.y - n.h / 2;
      cardOf.get(n.id).style.transform = 'translate(' + n.x + 'px, ' + n.y + 'px)';
    });
    const gg = g.graph();
    const hasLoop = edges.some(e => e.kind === 'writes_back');
    bounds = { w: gg.width || 0, h: (gg.height || 0) + (hasLoop ? LOOP_GAP + 12 : 0) };
    world.style.width = bounds.w + 'px';
    world.style.height = bounds.h + 'px';

    drawEdges(edges);
    applyFilter();
    highlight(sticky ? byId.get(sticky) : null);
    // The budget agreed for this page: 300 nodes under 200 ms.
    console.debug('lineage: laid out ' + rendered.length + ' nodes / ' + edges.length
      + ' edges in ' + (performance.now() - t0).toFixed(1) + ' ms');
  }

  function drawEdges(edges) {
    while (edgeSvg.firstChild) edgeSvg.removeChild(edgeSvg.firstChild);
    edgeSvg.setAttribute('width', bounds.w);
    edgeSvg.setAttribute('height', bounds.h);
    edgeSvg.setAttribute('viewBox', '0 0 ' + bounds.w + ' ' + bounds.h);
    const defs = document.createElementNS(NS, 'defs');
    const marker = document.createElementNS(NS, 'marker');
    marker.setAttribute('id', 'lin-arrow');
    marker.setAttribute('viewBox', '0 0 6 6');
    marker.setAttribute('refX', '5'); marker.setAttribute('refY', '3');
    marker.setAttribute('markerWidth', '5'); marker.setAttribute('markerHeight', '5');
    marker.setAttribute('orient', 'auto-start-reverse');
    const head = document.createElementNS(NS, 'path');
    head.setAttribute('d', 'M0 0 L6 3 L0 6 z');
    head.setAttribute('fill', 'currentColor');
    marker.appendChild(head);
    defs.appendChild(marker);
    edgeSvg.appendChild(defs);

    drawn = [];
    edges.forEach(e => {
      const a = e.a, b = e.b;
      const p = document.createElementNS(NS, 'path');
      let d;
      if (e.kind === 'writes_back') {
        // The only edge on this page that runs against the flow, so it must not
        // be mistakeable for one that runs with it: out the bottom, left below
        // everything, back into the folder's left side.
        const acx = a.x + a.w / 2, abot = a.y + a.h;
        const bx = b.x, bcy = b.y + b.h / 2;
        const yl = bounds.h - LOOP_GAP / 2;
        d = 'M' + acx + ' ' + abot + ' C' + acx + ' ' + yl + ', ' + (bx - 56) + ' ' + yl
          + ', ' + (bx - 56) + ' ' + bcy + ' L' + bx + ' ' + bcy;
      } else {
        const x1 = a.x + a.w, y1 = a.y + a.h / 2;
        const x2 = b.x, y2 = b.y + b.h / 2;
        const dx = Math.max(18, (x2 - x1) * 0.5);
        d = 'M' + x1 + ' ' + y1 + ' C' + (x1 + dx) + ' ' + y1 + ', ' + (x2 - dx) + ' ' + y2
          + ', ' + x2 + ' ' + y2;
      }
      p.setAttribute('class', 'lin-edge kind-' + e.kind + (e.via ? ' is-via' : ''));
      p.setAttribute('d', d);
      if (e.kind === 'writes_back') p.setAttribute('marker-end', 'url(#lin-arrow)');
      edgeSvg.appendChild(p);
      drawn.push({ el: p, a: a, b: b, edge: e });
    });
  }

  // -- Zoom / pan -----------------------------------------------------
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function applyView() {
    world.style.transform = 'translate(' + view.x + 'px, ' + view.y + 'px) scale(' + view.k + ')';
    if (zoomLbl) zoomLbl.textContent = Math.round(view.k * 100) + '%';
  }
  function zoomAt(k, cx, cy) {
    k = clamp(k, K_MIN, K_MAX);
    view.x = cx - (cx - view.x) * (k / view.k);
    view.y = cy - (cy - view.y) * (k / view.k);
    view.k = k;
    applyView();
  }
  function fit() {
    const r = viewport.getBoundingClientRect();
    if (!bounds.w || !bounds.h) return;
    // Never magnify to fit: a four-node graph blown up to 3x looks broken.
    const k = clamp(Math.min((r.width - 24) / bounds.w, (r.height - 24) / bounds.h), K_MIN, 1);
    view.k = k;
    view.x = (r.width - bounds.w * k) / 2;
    view.y = (r.height - bounds.h * k) / 2;
    applyView();
  }
  function reset() { view = { x: 16, y: 16, k: 1 }; applyView(); }

  // Figma/Miro semantics: a bare wheel (mouse wheel or two-finger trackpad
  // scroll) pans, because that is what people do reflexively to look around a
  // canvas; the zoom gesture is opt-in via a modifier. A trackpad pinch is
  // reported to the browser as a wheel event with ctrlKey set (there is no
  // separate "pinch" DOM event), so ctrl+wheel and a real pinch are the same
  // branch; meta+wheel rides along for anyone used to the Mac convention.
  viewport.addEventListener('wheel', ev => {
    ev.preventDefault();
    const r = viewport.getBoundingClientRect();
    if (ev.ctrlKey || ev.metaKey) {
      zoomAt(view.k * Math.exp(-ev.deltaY * 0.0015), ev.clientX - r.left, ev.clientY - r.top);
      return;
    }
    // deltaMode is almost always 0 (pixels); the 1 (line) case is a handful of
    // non-Chrome mice and is scaled up or a "line" would barely move the view.
    const ux = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? r.width : 1;
    const uy = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? r.height : 1;
    view.x -= ev.deltaX * ux;
    view.y -= ev.deltaY * uy;
    applyView();
  }, { passive: false });

  const DRAG_THRESHOLD = 4;   // px of slop before a press counts as a drag, not a click
  let panning = false, dragged = false, downOnBackground = false, px = 0, py = 0;
  viewport.addEventListener('pointerdown', ev => {
    if (ev.button !== 0) return;
    panning = true; dragged = false; px = ev.clientX; py = ev.clientY;
    // Record where the press started: once a drag captures the pointer,
    // pointerup retargets to the viewport and ev.target stops being useful.
    downOnBackground = ev.target === viewport || ev.target === world;
    // NOT captured here. Once a pointer is captured, Chrome dispatches the
    // following `click` to the capturing element, so a press on a card never
    // reached the card's click handler and the focus never stuck. Capture is
    // only needed to keep a drag alive outside the viewport - it is taken the
    // moment the press turns into a drag (pointermove below).
  });
  viewport.addEventListener('pointermove', ev => {
    if (!panning) return;
    const dx = ev.clientX - px, dy = ev.clientY - py;
    // Measured from the pointerdown anchor (px/py only move once dragged is
    // true), so many small moves in a row still accumulate toward the
    // threshold instead of each one being compared against the last.
    if (!dragged && Math.hypot(dx, dy) <= DRAG_THRESHOLD) return;
    if (!dragged && viewport.setPointerCapture) viewport.setPointerCapture(ev.pointerId);
    dragged = true;
    viewport.classList.add('is-panning');
    view.x += dx; view.y += dy; px = ev.clientX; py = ev.clientY;
    applyView();
  });
  function endPan(ev) {
    if (!panning) return;
    panning = false;
    viewport.classList.remove('is-panning');
    if (viewport.hasPointerCapture && viewport.hasPointerCapture(ev.pointerId)) {
      viewport.releasePointerCapture(ev.pointerId);
    }
    // A click on empty canvas clears the focus; a drag that ends there, or a
    // click that started on a card, must not. A cancelled press (browser takes
    // over a touch scroll, or anything else that interrupts the gesture) is
    // not a click either - only a genuine pointerup counts, or a one-finger
    // touch-scroll flick on empty canvas would silently wipe the focus.
    if (ev.type === 'pointerup' && !dragged && downOnBackground) setSticky(null);
    setTimeout(() => { dragged = false; }, 0);
  }
  viewport.addEventListener('pointerup', endPan);
  viewport.addEventListener('pointercancel', endPan);

  // Hovering the viewport also arms the shortcuts, not only focusing something
  // inside it: someone reading the graph with the mouse over it, nothing
  // tabbed to, should still get +/-/0 without stealing them from a filter
  // input elsewhere on the page.
  let hovering = false;
  viewport.addEventListener('mouseenter', () => { hovering = true; });
  viewport.addEventListener('mouseleave', () => { hovering = false; });
  function sectionIsActive() {
    return hovering || (section.contains && section.contains(document.activeElement));
  }

  // Bound on document, not section: with nothing inside the section focused,
  // a real keydown's target is outside it (body, or nothing at all) and would
  // never bubble up to a section-level listener, so the hover-only case above
  // would never fire.
  document.addEventListener('keydown', ev => {
    if (ev.target && /^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    // Escape is scoped to focus-inside only, never hover: a card's own keydown
    // handler (below) already clears sticky when Escape lands ON a card, so
    // this branch exists for Escape reaching the section from something else
    // focused inside it (e.g. a toolbar button). Arming it on hover too would
    // make Escape clear the lineage trace while dismissing unrelated UI - the
    // drawer's own Escape handler runs in the same keydown, and mouse position
    // is incidental to what the user meant to close.
    if (ev.key === 'Escape') {
      if (!section.contains(document.activeElement)) return;
      ev.preventDefault(); setSticky(null); return;
    }
    // +/-/0 stay armed on hover too: reading the graph with the mouse over it,
    // nothing tabbed to, should still zoom without stealing the keys from a
    // filter input elsewhere on the page.
    if (!sectionIsActive()) return;
    // Ctrl/Cmd+ +/-/0 are the BROWSER's own zoom-reset shortcuts; without this
    // guard hovering the graph (no focus needed) hijacks them too, resetting
    // the graph's view at the same time the browser resets its page zoom.
    // The wheel handler above already makes this same distinction via
    // ev.ctrlKey/ev.metaKey — this mirrors it for the keyboard path.
    if (ev.ctrlKey || ev.metaKey) return;
    const r = viewport.getBoundingClientRect();
    if (ev.key === '+' || ev.key === '=') { ev.preventDefault(); zoomAt(view.k * 1.2, r.width / 2, r.height / 2); }
    else if (ev.key === '-' || ev.key === '_') { ev.preventDefault(); zoomAt(view.k / 1.2, r.width / 2, r.height / 2); }
    else if (ev.key === '0') { ev.preventDefault(); fit(); }
  });

  section.querySelectorAll('[data-lin-act]').forEach(btn => {
    btn.addEventListener('click', () => {
      const a = btn.dataset.linAct;
      if (a === 'fit') fit();
      else if (a === 'reset') reset();
      else if (a === 'retry') load();
      else if (a === 'unfocus') setSticky(null);
    });
  });
  section.querySelectorAll('[data-lin-zoom]').forEach(btn => {
    btn.addEventListener('click', () => {
      const r = viewport.getBoundingClientRect();
      const z = btn.dataset.linZoom;
      if (z === 'in') zoomAt(view.k * 1.2, r.width / 2, r.height / 2);
      else if (z === 'out') zoomAt(view.k / 1.2, r.width / 2, r.height / 2);
      else if (z === '100') zoomAt(1, r.width / 2, r.height / 2);
    });
  });
  if (detailSel) {
    detailSel.addEventListener('change', () => {
      level = detailSel.value;
      // The select is the global switch, so it overrules per-pattern state:
      // "Sheets" has to mean sheets, whatever was folded by hand before. But
      // `collapsed` also holds each staged generator's own fold (P4.1's "a
      // stage chain starts folded" default) — that is a per-generator choice,
      // not a pattern fold the select owns, so only pattern ids are cleared.
      if (level === 'sheets') {
        Array.from(collapsed).forEach(id => {
          const n = byId.get(id);
          if (n && n.kind === 'pattern') collapsed.delete(id);
        });
      }
      render();
      fit();
    });
  }
  if (exportSel) {
    // A <select> rather than a link: the URL depends on the CURRENT sticky
    // focus, which changes after the page loads, so there is nothing to
    // point an <a href> at ahead of time. The browser's own download
    // handling (the route sends Content-Disposition: attachment) does the
    // rest - no blob/anchor dance needed.
    exportSel.addEventListener('change', () => {
      const format = exportSel.value;
      exportSel.value = '';               // a menu, not a persistent mode
      if (!format) return;
      const u = new URL('/api/data-sources/lineage', location.href);
      u.searchParams.set('format', format);
      if (sticky) u.searchParams.set('focus', sticky);
      location.href = u.toString();
    });
  }

  // -- Filters, drawer, table rows ------------------------------------
  function applyFilter() {
    if (!filterKeys) {
      section.classList.remove('is-filtered');
      cardOf.forEach(c => c.classList.remove('out'));
      drawn.forEach(d => d.el.classList.remove('out'));
      return;
    }
    section.classList.add('is-filtered');
    const live = new Set();
    bySrcKey.forEach((n, key) => {
      if (!filterKeys.has(key)) return;
      reach(n).forEach(id => live.add(id));
    });
    cardOf.forEach((c, id) => c.classList.toggle('out', !live.has(id)));
    drawn.forEach(d => d.el.classList.toggle('out', !(live.has(d.a.id) && live.has(d.b.id))));
  }

  document.addEventListener('ds:filter', e => {
    const keys = e.detail.keys;
    // "Everything is selected" is not a filter: it must not dim the graph, and
    // it arrives once on load, before the graph exists.
    let all = true;
    bySrcKey.forEach((n, k) => { if (!keys.has(k)) all = false; });
    filterKeys = all ? null : keys;
    applyFilter();
  });

  document.addEventListener('ds:saved', e => {
    const n = bySrcKey.get(e.detail.key);
    if (!n) return;
    n.data.declared_path = e.detail.source.declared_path;
    n.data.label = e.detail.source.label;
    const card = cardOf.get(n.id);
    if (card) paint(n, card);        // the "by name" chip retires at once
  });

  // The table below and the picture above are one artefact, not two views
  // ignoring each other.
  document.querySelectorAll('[data-src-row]').forEach(row => {
    row.addEventListener('mouseenter', () => {
      const n = bySrcKey.get(row.dataset.srcRow);
      if (n && !sticky) highlight(n);
    });
    row.addEventListener('mouseleave', () => { if (!sticky) highlight(null); });
  });

  // -- Load -----------------------------------------------------------
  function setVeil(which, msg) {
    if (skeleton) skeleton.hidden = which !== 'skeleton';
    if (errorEl) {
      errorEl.hidden = which !== 'error';
      const p = errorEl.querySelector('[data-lin-msg]');
      if (p && msg) p.textContent = msg;
    }
  }

  async function load() {
    setVeil('skeleton');
    let res = null, data = null;
    try {
      res = await fetch('/api/data-sources/lineage', { headers: { Accept: 'application/json' } });
    } catch (err) { res = null; }

    if (res && res.ok) {
      try { data = await res.json(); }
      catch (err) { setVeil('error', 'The server sent something this page could not read.'); return; }
    } else if (DEV) {
      // ?lineage=sample only. The fixture exists so the drawing can be built
      // against the pinned shape before the endpoint lands.
      data = SAMPLE;
      console.debug('lineage: server said ' + (res ? res.status : 'nothing')
        + ', drawing the sample fixture');
    } else if (res && res.status === 501) {
      setVeil('error', 'Lineage is not available on this server yet.');
      return;
    } else if (res) {
      setVeil('error', 'Could not load lineage (' + res.status + ').');
      return;
    } else {
      setVeil('error', 'Could not reach the server.');
      return;
    }

    build(data);
    if (!NODES.length) {
      setVeil('error', 'Nothing to draw yet - run a report once and its inputs appear here.');
      return;
    }
    if (typeof dagre === 'undefined') {
      setVeil('error', 'The layout engine did not load.');
      return;
    }
    setVeil(null);
    if (detailSel && detailSel.value) level = detailSel.value;
    render();
    fit();

    const want = new URL(location.href).searchParams.get('focus');
    if (want && byId.get(want)) {
      const n = byId.get(want);
      // A deep link into a folded sheet or stage has to unfold it, or it
      // points at a card that is not on the page — the app generates these
      // links itself (a stage card's own URL), so a dead one is self-inflicted.
      if ((n.kind === 'sheet' || n.kind === 'stage') && n.parent) {
        level = 'sheets';
        if (detailSel) detailSel.value = 'sheets';
        collapsed.delete(n.parent);
        render();
      }
      setSticky(want);
      const card = cardOf.get(want);
      if (card) card.focus({ preventScroll: true });
    }
  }

  let raf = 0;
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { if (rendered.length) applyView(); });
    }).observe(viewport);
  }

  // Development fixture, byte-for-byte tests/fixtures/lineage_sample.json.
  // Drawn only under ?lineage=sample; the server is the source of truth.
  const SAMPLE = {"meta":{"generated_at":"2026-08-20T10:00:00","runs_considered":5,"stale_days":14},"nodes":[{"id":"src:1","layer":0,"kind":"folder","label":"Stock_RDC","data":{"source_id":1,"declared_path":"\\\\share\\OPS\\RDC\\Stock_RDC","files":142,"bytes":546000000,"data_age_days":1,"read_age_days":0,"fingerprint":"9f1c…","shared":true,"data_stale":false,"unread":false}},{"id":"src:2","layer":0,"kind":"folder","label":"Reasons_Catalog","data":{"source_id":2,"declared_path":null,"files":1,"bytes":2100000,"data_age_days":40,"read_age_days":1,"fingerprint":"a0b3…","shared":false,"data_stale":true,"unread":false}},{"id":"src:3","layer":0,"kind":"greenplum","label":"gp_dwh","data":{"source_id":3,"queries":["sql/rejections.sql"],"shared":false}},{"id":"pat:rdcstock:Stock_*.xlsx","layer":1,"kind":"pattern","label":"Stock_*.xlsx","data":{"generator":"rdcstock","role":"data","required":false,"matched_files":140,"unmatched":false}},{"id":"pat:rdcstock:SL_NWD.xls","layer":1,"kind":"pattern","label":"SL_NWD.xls","data":{"generator":"rdcstock","role":"reference","required":true,"matched_files":1,"unmatched":false,"from_required_files":true}},{"id":"pat:opshealth:*","layer":1,"kind":"pattern","label":"*","data":{"generator":"opshealth","role":"data","required":false,"matched_files":142,"legacy":true}},{"id":"sheet:rdcstock:Stock_*.xlsx:Stock","layer":2,"kind":"sheet","label":"Stock","data":{"columns":[{"name":"SKU","declared":true,"observed":true,"consumed":true},{"name":"Qty","declared":true,"observed":true,"consumed":true},{"name":"Reason_ID","declared":true,"observed":false,"consumed":null},{"name":"Qty_new","declared":false,"observed":true,"consumed":null}],"observed_rows":184000,"diff":{"missing_columns":["Reason_ID"],"extra_columns":["Qty_new"]}}},{"id":"sheet:rdcstock:SL_NWD.xls:*","layer":2,"kind":"sheet","label":"*","data":{"columns":[{"name":"Reason_ID","declared":true,"observed":true,"consumed":null},{"name":"Reason_Name","declared":true,"observed":true,"consumed":null}],"diff":{}}},{"id":"gen:rdcstock","layer":3,"kind":"generator","label":"rdcstock","data":{"generator_id":7,"version":3,"engine":"browser","reports":1}},{"id":"gen:opshealth","layer":3,"kind":"generator","label":"opshealth","data":{"generator_id":8,"version":1,"engine":"browser","reports":1,"legacy_inputs":true}},{"id":"gen:dwhreject","layer":3,"kind":"generator","label":"dwhreject","data":{"generator_id":9,"version":2,"engine":"agent","reports":1,"observed":false}},{"id":"out:rdcstock:html","layer":4,"kind":"output","label":"dashboard.html","data":{"slot":null}},{"id":"out:rdcstock:0","layer":4,"kind":"output","label":"stock.bin.gz","data":{"slot":0}},{"id":"out:rdcstock:wb:sl_unassigned_reasons.xlsx","layer":4,"kind":"write_back","label":"sl_unassigned_reasons.xlsx","data":{"targets":["Stock_RDC"],"failed":[],"last_written":"2026-08-19"}},{"id":"out:opshealth:html","layer":4,"kind":"output","label":"dashboard.html","data":{"slot":null}},{"id":"out:dwhreject:html","layer":4,"kind":"output","label":"dashboard.html","data":{"slot":null}},{"id":"rep:d1","layer":5,"kind":"report","label":"RDC Stock Health","data":{"dashboard_id":"d1","status":"success","last_run_at":"2026-08-19","engine":"browser"}},{"id":"rep:d2","layer":5,"kind":"report","label":"OPS RDC Health","data":{"dashboard_id":"d2","status":"error","last_run_at":"2026-08-20","engine":"browser"}},{"id":"rep:d3","layer":5,"kind":"report","label":"DWH Rejection Analysis","data":{"dashboard_id":"d3","status":"success","last_run_at":"2026-08-17","engine":"agent"}}],"edges":[{"from":"src:1","to":"pat:rdcstock:Stock_*.xlsx","kind":"matches"},{"from":"src:2","to":"pat:rdcstock:SL_NWD.xls","kind":"matches"},{"from":"src:1","to":"pat:opshealth:*","kind":"matches"},{"from":"pat:rdcstock:Stock_*.xlsx","to":"sheet:rdcstock:Stock_*.xlsx:Stock","kind":"contains"},{"from":"pat:rdcstock:SL_NWD.xls","to":"sheet:rdcstock:SL_NWD.xls:*","kind":"contains"},{"from":"sheet:rdcstock:Stock_*.xlsx:Stock","to":"gen:rdcstock","kind":"reads"},{"from":"sheet:rdcstock:SL_NWD.xls:*","to":"gen:rdcstock","kind":"reads"},{"from":"pat:opshealth:*","to":"gen:opshealth","kind":"reads"},{"from":"src:3","to":"gen:dwhreject","kind":"reads"},{"from":"gen:rdcstock","to":"out:rdcstock:html","kind":"writes"},{"from":"gen:rdcstock","to":"out:rdcstock:0","kind":"writes"},{"from":"gen:rdcstock","to":"out:rdcstock:wb:sl_unassigned_reasons.xlsx","kind":"writes"},{"from":"out:rdcstock:wb:sl_unassigned_reasons.xlsx","to":"src:1","kind":"writes_back"},{"from":"gen:opshealth","to":"out:opshealth:html","kind":"writes"},{"from":"gen:dwhreject","to":"out:dwhreject:html","kind":"writes"},{"from":"out:rdcstock:html","to":"rep:d1","kind":"publishes"},{"from":"out:rdcstock:0","to":"rep:d1","kind":"publishes"},{"from":"out:opshealth:html","to":"rep:d2","kind":"publishes"},{"from":"out:dwhreject:html","to":"rep:d3","kind":"publishes"}]};

  load();
})();
