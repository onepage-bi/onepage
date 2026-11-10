/* Explore — the "big gun" for operations: connect a folder, index it in the
 * browser (gen-worker.js in 'explore' mode), build/preview/run SQL over the
 * in-memory DuckDB, run one of 5 recipes, ask a question in Russian
 * (server-side NL→SQL, never auto-run), inspect the result grid, save the
 * question or export to Excel.
 *
 * Same idiom as static/js/catalog.js / data-quality.js: one in-memory
 * snapshot (the catalog the server baked into #ex-root), a page-local el()
 * helper, JSON in data-attrs. The worker protocol is the SHARED CONTRACT —
 * this file only sends/receives the documented message shapes; it never
 * assumes anything about how gen-worker.js parses files internally.
 */
(function () {
  'use strict';

  function parseJson(raw, fallback) {
    try { return JSON.parse(raw || ''); } catch (e) { return fallback; }
  }

  function tableByIdIn(tables, id) { return (tables || []).find(tv => tv.table_id === id) || null; }

  // ═════════════════════════════════════════════════════════════════
  // SQL quoting + builder (pure — exported for node tests, no DOM/window
  // touched anywhere in this block)
  // ═════════════════════════════════════════════════════════════════
  function qid(name) { return '"' + String(name == null ? '' : name).replace(/"/g, '""') + '"'; }
  function qlit(v) { return "'" + String(v == null ? '' : v).replace(/'/g, "''") + "'"; }

  const NUM_OPS = ['=', '!=', '>', '>=', '<', '<='];
  function looksNumeric(v) { return /^-?\d+(\.\d+)?$/.test(String(v).trim()); }

  // `columnTypes` is the catalog's per-column type ('number'/'text'/'date'/…,
  // see static/py/_lineage.py:_profile_out) — the authority on whether a
  // digits-only value is a number or a text/code column ("007"). Falling
  // back to the value's own shape (no type known) still guards the common
  // case; it is never used when the type says otherwise.
  function filterClause(f, columnTypes) {
    const col = qid(f.column);
    if (f.op === 'IS NULL' || f.op === 'IS NOT NULL') return col + ' ' + f.op;
    if (f.op === 'IN') {
      const vals = String(f.value == null ? '' : f.value).split(',').map(s => qlit(s.trim())).filter(s => s !== "''");
      return col + ' IN (' + (vals.length ? vals.join(', ') : "''") + ')';
    }
    if (f.op === 'LIKE') return col + ' LIKE ' + qlit('%' + (f.value == null ? '' : f.value) + '%');
    const type = columnTypes && columnTypes[f.column];
    const isNumericCol = type ? type === 'number' : looksNumeric(f.value);
    const val = (NUM_OPS.includes(f.op) && isNumericCol) ? String(f.value).trim() : qlit(f.value);
    return col + ' ' + f.op + ' ' + val;
  }

  // Aggregate function labels shown to the user — every UI string is
  // Russian, and the result grid renders these as literal <th> text.
  const AGG_LABEL = { COUNT: t('Qty'), SUM: t('Sum'), AVG: t('Average'), MIN: t('Min'), MAX: t('Max') };
  function aggExpr(fn, col) {
    if (!fn) return null;
    if (fn === 'COUNT' && !col) return 'COUNT(*) AS ' + qid(t('Qty'));
    const label = (AGG_LABEL[fn] || fn) + '_' + String(col || 'x');
    return fn + '(' + qid(col) + ') AS ' + qid(label);
  }

  // spec: { table, columns:[], filters:[{column,op,value}], groupBy:[],
  //         aggFn, aggCol, orderCol, orderDir, limit }
  function buildSql(spec) {
    if (!spec || !spec.table) return '';
    const groupBy = (spec.groupBy || []).filter(Boolean);
    const agg = aggExpr(spec.aggFn, spec.aggCol);
    let selectCols;
    if (groupBy.length || agg) {
      selectCols = groupBy.map(qid).concat(agg ? [agg] : []);
      if (!selectCols.length) selectCols = ['*'];
    } else {
      const cols = (spec.columns || []).filter(Boolean);
      selectCols = cols.length ? cols.map(qid) : ['*'];
    }
    let sql = 'SELECT ' + selectCols.join(', ') + '\nFROM ' + qid(spec.table);
    const clauses = (spec.filters || []).filter(f => f && f.column && f.op)
      .map(f => filterClause(f, spec.columnTypes));
    if (clauses.length) sql += '\nWHERE ' + clauses.join(' AND ');
    if (groupBy.length) sql += '\nGROUP BY ' + groupBy.map(qid).join(', ');
    if (spec.orderCol) sql += '\nORDER BY ' + qid(spec.orderCol) + ' ' + (spec.orderDir === 'DESC' ? 'DESC' : 'ASC');
    const limit = Number(spec.limit);
    if (limit > 0) sql += '\nLIMIT ' + Math.min(limit, 1000);
    return sql;
  }

  // ═════════════════════════════════════════════════════════════════
  // File → catalog partition matching (pure)
  // ═════════════════════════════════════════════════════════════════
  // Builds the worker's `files` array from a flat directory listing
  // ({name,size,mtime_ms}) matched against every partition of every table
  // by (file name [+ sheet]). One physical file can back several table
  // rows (several sheets) — each becomes its own worker file record.
  function matchFilesToCatalog(files, tables) {
    const byName = new Map();
    (files || []).forEach(f => byName.set(f.name, f));
    const matched = [];
    const matchedNames = new Set();
    (tables || []).forEach(tv => {
      (tv.partitions || []).forEach(p => {
        const f = byName.get(p.file);
        if (!f) return;
        matchedNames.add(p.file);
        matched.push({
          dir: 0, name: f.name, size: f.size, mtime_ms: f.mtime_ms,
          table_id: tv.table_id, sheet: p.sheet || null, partition: p.key || null,
          header_row: tv.header_row || 0,
        });
      });
    });
    const unmatched = (files || []).filter(f => !matchedNames.has(f.name)).map(f => f.name);
    return { matched, unmatched };
  }

  // ═════════════════════════════════════════════════════════════════
  // Period arithmetic (pure) — the JS half of nl2sql.shift_period.
  // ═════════════════════════════════════════════════════════════════
  // A "why did SL drop in January?" card needs the SAME metric one period
  // earlier, and the only thing that says what "one period earlier" means is
  // the intent's own time filters. The rules are identical to the server's
  // (app/services/nl2sql.py:shift_period) and pinned by both test suites:
  // the FINEST time dim present steps back and carries into the coarser ones
  // (month 1 → year-1/month 12, week 1 → year-1/week 52, day → the previous
  // calendar day); non-time filters ride along untouched; only `=` filters
  // can shift, because an `in` over three months has no one predecessor.
  const SHIFT_DIMS = ['day', 'week', 'month', 'year'];
  const ISO_DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

  function shiftPeriod(filters) {
    const src = (filters || []).filter(f => f && typeof f === 'object').map(f => Object.assign({}, f));
    const idx = {};
    src.forEach((f, i) => {
      const dim = String(f.dim || '');
      if (SHIFT_DIMS.indexOf(dim) >= 0 && String(f.op || '=') === '=') idx[dim] = i;
    });
    if (!Object.keys(idx).length) return null;
    const num = dim => {
      const v = parseInt(String(src[idx[dim]].value).trim(), 10);
      return isNaN(v) ? null : v;
    };
    const set = (dim, v) => { src[idx[dim]].value = v; };
    const yearBack = () => {
      if (!('year' in idx)) return src;      // no year named — nothing to carry
      const y = num('year');
      if (y == null) return null;
      set('year', y - 1);
      return src;
    };
    if ('day' in idx) return shiftDay(src, idx);
    if ('week' in idx) {
      const w = num('week');
      if (w == null) return null;
      if (w > 1) { set('week', w - 1); return src; }
      set('week', 52);
      return yearBack();
    }
    if ('month' in idx) {
      const m = num('month');
      if (m == null) return null;
      if (m > 1) { set('month', m - 1); return src; }
      set('month', 12);
      return yearBack();
    }
    const y = num('year');
    if (y == null) return null;
    set('year', y - 1);
    return src;
  }

  function shiftDay(src, idx) {
    const raw = String(src[idx.day].value == null ? '' : src[idx.day].value).trim();
    const m = ISO_DAY_RE.exec(raw);
    if (m) {
      const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) - 86400000);
      if (isNaN(d.getTime())) return null;
      const p2 = n => (n < 10 ? '0' : '') + n;
      src[idx.day].value = d.getUTCFullYear() + '-' + p2(d.getUTCMonth() + 1) + '-' + p2(d.getUTCDate());
      if ('month' in idx) src[idx.month].value = d.getUTCMonth() + 1;
      if ('year' in idx) src[idx.year].value = d.getUTCFullYear();
      return src;
    }
    const day = parseInt(raw, 10);
    if (isNaN(day)) return null;
    const year = 'year' in idx ? parseInt(String(src[idx.year].value).trim(), 10) : NaN;
    const month = 'month' in idx ? parseInt(String(src[idx.month].value).trim(), 10) : NaN;
    if (!isNaN(year) && !isNaN(month)) {
      const d = new Date(Date.UTC(year, month - 1, day) - 86400000);
      if (isNaN(d.getTime())) return null;
      src[idx.day].value = d.getUTCDate();
      src[idx.month].value = d.getUTCMonth() + 1;
      src[idx.year].value = d.getUTCFullYear();
      return src;
    }
    if (day > 1) { src[idx.day].value = day - 1; return src; }
    // Day 1 with no year/month to fall back into: the previous month's
    // length is unknowable from the filters alone.
    return null;
  }

  // ═════════════════════════════════════════════════════════════════
  // Phase 4.5 (pure) — "why is the number in the report 25.5?" asked cold
  // ═════════════════════════════════════════════════════════════════
  // A why-question that carries a NUMBER and names no metric cannot be routed
  // by the model: cell values never reach the server (hard boundary), so
  // «25,5» is meaningless there. The data, however, is in THIS browser's
  // engine — so the lookup is client-side, and these are its pure halves.

  // "why …" / "what for …" / "because of what …" — the client's own pre-check,
  // deliberately narrower than the server classifier: it only has to be right
  // about the questions it INTERCEPTS. Anything it declines keeps the existing
  // server route untouched.
  // `\b` is ASCII-only in JS (Cyrillic is not `\w`), so the word end is spelt
  // out as a negative lookahead — with `\b` this regex never matched a single
  // Russian question.
  const WHY_HEAD_RE = /^\s*(почему|зачем|из-за\s+чего|отчего|что\s+случилось|что\s+произошло)(?![a-zа-я])/i;
  // "Trend": a question about CHANGE over recent periods — "which chain has
  // a negative trend over the last two months?". Answered with two
  // periods side by side per cut and the delta, never with a flat list
  // truncated by LIMIT (the defect that made this route: «month >= 5 …
  // ORDER BY month LIMIT 10» showed ten rows of ONE month).
  const TREND_RE = /тренд|динамик|раст[её]т|растут|пада[её]т|падают|снижа|снизил|ухудш|улучш|изменил|изменен|вырос|упал/i;
  const RECENT_RE = /последн[а-яё]*\s+(\d+|дв[ае]|тр[иё]х?|четыр[её]х?|пят[иь]|шест[иь])?\s*(месяц|недел|дн|год)/i;
  // "by year / for the year / since the start of the year / this year / YTD": a trend over the
  // whole year is month-by-month, first month of data against the latest —
  // not the last two months, which is what a bare "trend" compares.
  const YEAR_SPAN_RE = /(?:^|[^а-яё])(?:по|за|в)\s+(?:этом[у]?\s+|текущ[а-яё]+\s+)?год[ау]?(?![а-яё])|с\s+начала\s+года|(?:^|[^a-z])ytd(?![a-z])/i;
  const NUM_WORDS = { 'два': 2, 'две': 2, 'три': 3, 'трёх': 3, 'трех': 3, 'четыре': 4, 'четырёх': 4, 'четырех': 4, 'пять': 5, 'пяти': 5, 'шесть': 6, 'шести': 6 };
  // A question that NAMES a period ("in week 33", "in July", "2026") is
  // about that period, not about a trend — and a why-question is a why.
  // (`\b` is ASCII-only in JS regexes: Cyrillic words need explicit edges.)
  const NAMED_PERIOD_RE = /\d{1,2}\s*-?\s*(?:й|я|ой|ую|ая|ый)?\s*недел|недел[а-яё]*\s*(?:№\s*)?\d{1,2}|(?:^|[^а-яё])(январ|феврал|март|апрел|ма[яе](?![а-яё])|июн|июл|август|сентябр|октябр|ноябр|декабр)|(?:^|\D)20\d\d(?!\d)/i;
  function isTrendQuestion(question) {
    const q = String(question || '');
    if (isWhyQuestion(q)) return false;
    if (NAMED_PERIOD_RE.test(q)) return false;
    return TREND_RE.test(q) || RECENT_RE.test(q);
  }
  // {gran: 'month'|'week'|'day'|'year', n} — how many recent periods the
  // question asks about. A trend needs two points, so n is at least 2.
  function recentSpec(question) {
    const q = String(question || '').toLowerCase();
    const m = RECENT_RE.exec(q);
    let gran = 'month', n = 2;
    if (m) {
      const unit = m[2] || '';
      gran = unit.startsWith('недел') ? 'week' : unit.startsWith('дн') ? 'day' : unit.startsWith('год') ? 'year' : 'month';
      const raw = (m[1] || '').trim();
      n = raw ? (Number(raw) || NUM_WORDS[raw] || 2) : 1;
    } else if (YEAR_SPAN_RE.test(q)) { gran = 'month'; n = 12; }
    else if (/недел/.test(q)) gran = 'week';
    else if (/\bдн(ей|я|и)\b/.test(q)) gran = 'day';
    return { gran: gran, n: Math.max(2, Math.min(12, n)) };
  }
  function isWhyQuestion(question) {
    const q = String(question == null ? '' : question).toLowerCase().replace(/ё/g, 'е');
    if (WHY_HEAD_RE.test(q)) return true;
    // "and why …", "and why suddenly …" — the word inside the first three.
    return q.split(/[^a-zа-я0-9]+/).filter(Boolean).slice(0, 3).indexOf('почему') >= 0;
  }

  // Words that make an adjacent bare integer a PERIOD, not a value.
  const PERIOD_UNIT_STEMS = ['недел', 'дн', 'день', 'сутк', 'числ', 'месяц', 'год', 'лет', 'квартал'];
  function isPeriodUnit(w) {
    return !!w && PERIOD_UNIT_STEMS.some(s => w.indexOf(s) === 0);
  }

  // `25,5` / `25.5` / `25,50` / `25,5%` / `1 234,5` (thousands separated by a
  // space, an NBSP, or the U+2009 THIN SPACE this page's own `fmtNum` prints —
  // the operator's number is usually copied straight out of the result grid)
  // → {value, decimals, percent, raw}. The period parse runs FIRST and eats
  // its own tokens: a `20\d\d` year, and a bare integer sitting next to
  // a week/day/date-of-month unit word, are never value candidates. Two candidates (or none)
  // → null: an ambiguous number is not something to guess about, and the
  // question falls through to the existing routing.
  const NUM_TOKEN_RE =
    /([-−]?)(\d{1,3}(?:[    ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(\s*%)?|([a-zа-я]+)/g;
  const ORDINAL_TAIL_RE = /^-(й|я|е|м|го|му|ый|ая|ое|ю)/;

  function parseAskedNumber(question) {
    const src = String(question == null ? '' : question).toLowerCase().replace(/ё/g, 'е');
    const toks = [];
    let m;
    NUM_TOKEN_RE.lastIndex = 0;
    while ((m = NUM_TOKEN_RE.exec(src)) !== null) {
      if (m[4] != null) { toks.push({ kind: 'word', text: m[4] }); continue; }
      // A sign counts only when nothing alphanumeric precedes it: «-25,5» yes,
      // «20-25» (a range) no — there the «-» belongs to the previous number.
      const before = m.index > 0 ? src[m.index - 1] : '';
      const neg = !!m[1] && !/[a-zа-я0-9]/.test(before);
      toks.push({ kind: 'num', text: m[2], neg: neg, percent: !!m[3], end: m.index + m[0].length });
    }
    const cands = [];
    toks.forEach((tv, i) => {
      if (tv.kind !== 'num') return;
      // "2nd", "25th" — an ordinal is a position, never a measured value.
      if (ORDINAL_TAIL_RE.test(src.slice(tv.end))) return;
      const digits = tv.text.replace(/[    ]/g, '');
      const dot = digits.replace(',', '.');
      const frac = dot.indexOf('.') >= 0 ? dot.split('.')[1] : '';
      if (!frac && !tv.percent) {
        if (/^20\d\d$/.test(digits)) return;                       // "for 2026"
        const prev = toks[i - 1], next = toks[i + 1];
        // "number 96" is the flagship phrasing — the "date of month" stem disqualifies only when
        // it FOLLOWS the number ("the 5th" = the 5th day of the month).
        if ((prev && prev.kind === 'word' && isPeriodUnit(prev.text) && prev.text.indexOf('числ') !== 0)
          || (next && next.kind === 'word' && isPeriodUnit(next.text))) return;   // "in week 25"
      }
      const value = parseFloat(dot) * (tv.neg ? -1 : 1);
      if (!isFinite(value)) return;
      cands.push({ value: value, decimals: frac.length, percent: !!tv.percent,
        raw: (tv.neg ? '-' : '') + String(tv.text).trim() + (tv.percent ? '%' : '') });
    });
    return cands.length === 1 ? cands[0] : null;
  }

  // «25,5» was written with one decimal, so it means 25.46…25.54; a bare «96»
  // means 95.5…96.5. Half of the last written digit, and nothing wider — the
  // operator's own precision is the tolerance.
  const NUM_EPS = 1e-9;
  function numberTolerance(decimals) { return 0.5 * Math.pow(10, -(Number(decimals) || 0)); }

  // A ratio KPI stores 0,255 and displays «25,5 %», so both readings of a
  // candidate are tried. Ambiguity that creates (0,255 AND 25,5 both present)
  // is reported as ambiguity by the caller, never silently resolved.
  function numberMatches(candidate, parsed, ratio) {
    if (!parsed || candidate == null || typeof candidate === 'boolean') return false;
    const c = typeof candidate === 'number' ? candidate
      : Number(String(candidate).replace(/[\s\u00a0\u2009\u202f%]/g, '').replace(',', '.'));
    if (!isFinite(c)) return false;
    const tol = numberTolerance(parsed.decimals) + NUM_EPS;
    if (Math.abs(c - parsed.value) <= tol) return true;
    // The ×100 reading only for a ratio KPI (or a «%» in the question) —
    // otherwise «1000» would also claim any metric sitting at 10.
    return !!(ratio || parsed.percent) && Math.abs(c * 100 - parsed.value) <= tol;
  }

  const NUMERIC_CELL_RE = /^[-+]?[\d    ]+([.,]\d+)?\s*%?$/;
  function scanRowsForValue(rows, parsed, ratio) {
    return (rows || []).some(r => (r || []).some(c => {
      if (c == null || typeof c === 'boolean') return false;
      if (typeof c !== 'number' && !NUMERIC_CELL_RE.test(String(c).trim())) return false;
      return numberMatches(c, parsed, ratio);
    }));
  }

  // Node test hook only (tests/explore_dom.js) — exported unconditionally,
  // before anything below touches `document`/`window`, so the pure builder
  // and matcher can be required without a DOM.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { buildSql, matchFilesToCatalog, qid, qlit, filterClause, shiftPeriod,
      isWhyQuestion, parseAskedNumber, numberTolerance, numberMatches, scanRowsForValue,
      isTrendQuestion, recentSpec };
  }

  if (typeof document === 'undefined') return; // node require() path stops here

  const root = document.getElementById('ex-root');
  if (!root) return;

  const DATA = Object.assign({ tables: [], values: {} }, parseJson(root.dataset.catalog, {}));
  // Screen A's report strip: the generator-backed dashboards this user may
  // run (server-side reach, baked in like the catalog snapshot).
  const REPORTS = parseJson(root.dataset.reports, []) || [];
  // Folder tools are a SERVICE mode: `?folder=1`, or a catalogue with no
  // report cards at all. Everyone else loads the report's index, which the
  // run built over ALL its folders.
  const FOLDER_MODE = (function () {
    try { return new URLSearchParams(location.search).get('folder') === '1' || REPORTS.length === 0; }
    catch (e) { return REPORTS.length === 0; }
  })();
  const ASSET_V = root.dataset.assetV || '';
  const PYODIDE_BASE = root.dataset.pyodideBase || '';
  const STATIC_BASE = root.dataset.staticBase || '/static/';

  // ── Which engine answers this page (workstream H1) ─────────────────
  // Two workers, ONE protocol. `gen-worker.js` in explore mode runs
  // _explore.py inside Pyodide; `explore2/worker.js` runs the Rust parser +
  // duckdb-wasm with no Python. Everything below this block is written
  // against the message shapes, not against either engine — which is why the
  // switch is a URL and nothing else.
  //
  // `?engine=1|2` beats the server default (`OP_EXPLORE_ENGINE`), so an
  // operator can try v2 on one tab without a redeploy and fall back by
  // dropping the query string.
  const ENGINE = (function () {
    let q = null;
    try { q = new URLSearchParams(location.search).get('engine'); } catch (e) { q = null; }
    if (q === '1' || q === '2') return Number(q);
    return String(root.dataset.engine) === '2' ? 2 : 1;
  })();
  const WORKER_URL = (ENGINE === 2 ? '/static/js/explore2/worker.js' : '/static/js/gen-worker.js')
    + '?v=' + encodeURIComponent(ASSET_V);
  const USER_ID = (window.OP_USER && (window.OP_USER.id != null ? window.OP_USER.id : window.OP_USER.person_id)) || 'anon';
  // Folder handles are stored PER REPORT: every report is built from its own
  // source folder, so picking a report card switches the folder with it.
  // `explore:<user>` is the legacy single key (first release) — read as a
  // fallback, written alongside so older pages keep working.
  const HANDLE_KEY = 'explore:' + USER_ID;
  function handleKeyFor(gen) { return gen == null ? HANDLE_KEY : HANDLE_KEY + ':' + String(gen); }

  // ── tiny DOM helper, same idiom as catalog.js ───────────────────────
  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === 'text') { n.textContent = attrs[k]; continue; }
      if (k === 'class') n.className = attrs[k];
      // 'list' is a getter-only IDL property on <input> (its setter belongs
      // to the reflected attribute, not the property) — assigning `n.list =`
      // throws in strict mode. Route it through setAttribute like 'for'.
      else if (k in n && k !== 'for' && k !== 'list') n[k] = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(c => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }

  function tableById(id) { return tableByIdIn(DATA.tables, id); }
  function tableByName(id) { return tableById(id); }

  // ═════════════════════════════════════════════════════════════════
  // IndexedDB — persisted read-only folder handle (op-gen / handles)
  // ═════════════════════════════════════════════════════════════════
  function idbOpen() {
    return new Promise((res, rej) => {
      let req;
      try { req = indexedDB.open('op-gen', 1); } catch (e) { rej(e); return; }
      req.onupgradeneeded = () => { req.result.createObjectStore('handles'); };
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
  }
  async function idbGetHandle(key) {
    const db = await idbOpen();
    return new Promise((res, rej) => {
      const r = db.transaction('handles', 'readonly').objectStore('handles').get(key || HANDLE_KEY);
      r.onsuccess = () => res(r.result || null);
      r.onerror = () => rej(r.error);
    });
  }
  async function idbPutHandle(h, key) {
    const db = await idbOpen();
    return new Promise((res, rej) => {
      const tv = db.transaction('handles', 'readwrite');
      tv.objectStore('handles').put(h, key || HANDLE_KEY);
      tv.oncomplete = () => res();
      tv.onerror = () => rej(tv.error);
    });
  }
  // All of this user's stored folders at once: {key -> handle}. Feeds the
  // report cards ("folder '2026'" / "folder not connected") without opening
  // a transaction per card.
  async function idbAllHandles() {
    const db = await idbOpen();
    return new Promise((res, rej) => {
      const store = db.transaction('handles', 'readonly').objectStore('handles');
      const out = {};
      const r = store.openCursor();
      r.onsuccess = () => {
        const c = r.result;
        if (!c) { res(out); return; }
        if (typeof c.key === 'string' && c.key.startsWith(HANDLE_KEY)) out[c.key] = c.value;
        c.continue();
      };
      r.onerror = () => rej(r.error);
    });
  }

  // ═════════════════════════════════════════════════════════════════
  // Elements
  // ═════════════════════════════════════════════════════════════════
  const statusEl = document.getElementById('ex-status');
  const abortBtn = document.getElementById('ex-abort-btn');
  const connectBtn = document.getElementById('ex-connect-btn');
  const reconnectBtn = document.getElementById('ex-reconnect-btn');
  const indexBtn = document.getElementById('ex-index-btn');
  const progressWrap = document.getElementById('ex-progress');
  const progressFill = document.getElementById('ex-progress-fill');
  const progressMsg = document.getElementById('ex-progress-msg');
  const summaryEl = document.getElementById('ex-summary');
  // The folder/index controls live in a folded <details>; anything that
  // needs the operator's eyes (progress, a permission ask) unfolds it.
  function openSources() {
    const d = document.getElementById('ex-sources');
    if (d && 'open' in d) d.open = true;
  }

  const bTable = document.getElementById('ex-b-table');
  const bCols = document.getElementById('ex-b-cols');
  const bFilters = document.getElementById('ex-b-filters');
  const bFilterAdd = document.getElementById('ex-b-filter-add');
  const bGroupBy = document.getElementById('ex-b-groupby');
  const bAggFn = document.getElementById('ex-b-agg-fn');
  const bAggCol = document.getElementById('ex-b-agg-col');
  const bOrderCol = document.getElementById('ex-b-order-col');
  const bOrderDir = document.getElementById('ex-b-order-dir');
  const bLimit = document.getElementById('ex-b-limit');

  const sqlBox = document.getElementById('ex-sql');
  const runBtn = document.getElementById('ex-run-btn');
  const saveBtn = document.getElementById('ex-save-btn');
  const exportBtn = document.getElementById('ex-export-btn');

  const nlBox = document.getElementById('ex-nl');
  const nlBtn = document.getElementById('ex-nl-btn');
  const nlStatus = document.getElementById('ex-nl-status');

  const saveTitle = document.getElementById('ex-save-title');
  const sqlStatus = document.getElementById('ex-sql-status');
  const savedList = document.getElementById('ex-saved-list');
  const savedEmpty = document.getElementById('ex-saved-empty');

  const recipeList = document.getElementById('ex-recipe-list');
  const metricHead = document.getElementById('ex-metrics-head');
  const metricList = document.getElementById('ex-metric-list');
  // Phase 4.5: the disambiguation / fallback chips of a "why <number>?"
  // lookup. Its own container so it can be cleared without touching the
  // metrics panel, which is a different surface with a different lifetime.
  const valueChips = document.getElementById('ex-value-chips');
  const metricForm = document.getElementById('ex-metric-form');
  const recipeForm = document.getElementById('ex-recipe-form');

  const resultBadge = document.getElementById('ex-result-badge');
  const resultMs = document.getElementById('ex-result-ms');
  const resultThead = document.getElementById('ex-result-thead');
  const resultTbody = document.getElementById('ex-result-tbody');
  const resultEmpty = document.getElementById('ex-result-empty');

  const reportStrip = document.getElementById('ex-report-strip');
  const answerTitle = document.getElementById('ex-answer-title');
  const howBox = document.getElementById('ex-how');
  const howDef = document.getElementById('ex-how-def');
  const howCalc = document.getElementById('ex-how-calc');
  const howSources = document.getElementById('ex-how-sources');
  const sqlRow = document.getElementById('ex-row-sql');

  // ═════════════════════════════════════════════════════════════════
  // State
  // ═════════════════════════════════════════════════════════════════
  const state = {
    dirHandle: null,
    indexed: false,
    lastFiles: [],
    filterRows: [],
    lastResult: null,
    sortCol: null, sortDir: 1,
    maxMtime: 0, unmatched: [], workerTables: [],
    // Phase 9.4: where what the engine holds actually CAME from —
    // 'folder' (this browser read the workbooks) or 'server' (the report's
    // published lake index was downloaded and imported). Null until the
    // first successful index. `serverRun`/`serverParts` describe the run
    // behind a 'server' index, and every provenance line quotes them.
    source: null, serverRun: null, serverParts: 0,
    // ADR-14b: HOW queries are executed — null/'local' = the in-browser
    // worker; 'server' = `POST …/lake/exec` runs the same engine natively
    // over the published lake (no wasm boot, no partition download).
    // `execDash` is the dashboard whose lake answers.
    exec: null, execDash: null,
    // {generator_id -> manifest | false}. `false` = asked, none published;
    // an absent key = not asked yet. Filled lazily AFTER first paint.
    serverManifests: {},
    // Screen A: which report card is chosen (its generator wins over the
    // word-overlap guess), and what the answer card is currently showing.
    reportGen: null,
    // True while an index (folder or server) is being built. The chat's
    // folder pill reads THIS, not the button's disabled flag — a dropped
    // engine (timeout, "Stop") leaves the button off but is not
    // "still indexing".
    indexing: false,
    // {generator_id -> folder name} for the cards; filled from IndexedDB.
    foldersByGen: {},
    lastIntent: null, lastKpi: null, lastAnswer: null, lastNotes: [],
    lastUsed: [], pendingAnswer: null,
    // Phase 3 "Chat": what the answer card is showing RIGHT NOW — the
    // follow-up matcher's `prevIntent` is this, not merely the last answer
    // computed (opening bubble #7 from the thread makes #7 the open answer).
    // `explore-chat.js` writes it when it rehydrates a record from IndexedDB;
    // `runQuery`/`whyCard` write it for a freshly computed answer.
    openAnswer: null,
    // The question text the NEXT answer belongs to. Set by `ask()` and by the
    // metrics form; a bare SQL run leaves it null and falls back to the box.
    // `askSource`/`askParentN` ride along for the thread record: which button
    // produced this answer ('ask' | 'followup' | 'why' | 'compare') and which
    // earlier record it hangs off.
    askQuestion: null, askSource: null, askParentN: null,
    // Which ROUTE settled the current answer ('model' | 'form'): the thread
    // badge must say "with a model" when a model actually chose the intent,
    // whatever button asked the question.
    askVia: null,
    // P3a: WHAT the router knew when it settled this answer — the form
    // matcher's `match` ({score, runner_up, form}) and the ranked `shortlist`
    // it chose from. The chat's tier badge and its "Clarify" alternatives
    // are both read off these, so they ride to `emit('answer')` rather than
    // being recomputed from the question text.
    askMatch: null, askShortlist: null,
    // "Regenerate": the same question asked again on purpose. Rides
    // along so the thread can supersede the card it replaces.
    askRegen: false,
    // "Cancel" on the pending card. Checked twice — before the engine is
    // asked to execute, and before the answer is emitted — so a cancelled
    // question neither burns the worker nor lands as a record.
    askCancelled: false,
    // … and the request that is in flight RIGHT NOW is really aborted: the
    // slow half of a question is the model / the intent round trip, and a
    // "Cancel" that only sets a flag leaves it running (and paid for).
    // Not `killWorker`: that would drop the index with the query and cost a
    // 30 s rebuild — the engine is stopped by the two gates instead.
    askAbort: null,
    // What the engine holds right now ({tables, partitions, rows, bytes}),
    // frozen at the last applyIndexed. The chat's coverage pill is computed
    // from THIS and `workerTables` — no new request, ever.
    indexStats: {},
    // Phase 4.5 "why <number>?": `indexGen` bumps on every completed
    // `explore:indexed` (the ONE signal that says the engine now holds
    // different data — `applyIndexed` is its only handler), and it is the
    // cache key of the Grand-Total list a value lookup builds. Re-indexing the
    // folder therefore invalidates that list by key rotation, the same
    // discipline the render cache uses.
    indexGen: 0, gtCache: null,
    // Set by explore-chat.js: () -> Promise<record[]> for the OPEN thread.
    // Without the chat script the value lookup simply has no history to scan.
    historyProvider: null,
  };

  // ═════════════════════════════════════════════════════════════════
  // Answer events (phase 3) — the smallest listener registry that works.
  // `explore-chat.js` subscribes to 'answer' and writes one thread record
  // per completed answer; nothing in THIS file reads the registry back, so a
  // page without the chat script behaves exactly as it did before.
  // ═════════════════════════════════════════════════════════════════
  const listeners = {};
  function on(evt, fn) {
    if (typeof fn !== 'function') return () => {};
    (listeners[evt] = listeners[evt] || []).push(fn);
    return () => { listeners[evt] = (listeners[evt] || []).filter(f => f !== fn); };
  }
  // A listener that throws must not take the answer down with it: the answer
  // is already on screen by the time this runs, and a broken thread record is
  // strictly less bad than a broken page.
  function emit(evt, payload) {
    (listeners[evt] || []).slice().forEach(fn => {
      try { fn(payload); } catch (e) { /* a subscriber's problem, not ours */ }
    });
  }
  // Node test hook only (tests/explore_dom.js) — `state` is otherwise fully
  // closured inside this IIFE, and the hard-timeout regression test needs to
  // read `indexed`/`dirHandle` directly rather than inferring them from DOM.
  try { window.__opExploreState = state; } catch (e) { /* no window */ }

  let worker = null;
  let msgSeq = 0;
  const pending = new Map(); // id -> {resolve, reject}

  // The worker boots Pyodide+DuckDB lazily on the first 'explore:init'; the
  // page must not send index/sql before that init has been acknowledged with
  // 'explore:ready', or the worker throws "engine not initialised yet".
  let readyResolve = null;
  let readyPromise = null;

  function ensureWorker() {
    if (worker) return worker;
    worker = new Worker(WORKER_URL, { type: 'module' });
    worker.onmessage = onWorkerMessage;
    worker.onerror = (ev) => {
      const text = t('Engine error: {p1}', {p1: ((ev && ev.message) || t('unknown'))});
      setSummary(text);
      pending.forEach(p => p.resolve({ error: text }));
      pending.clear();
      if (readyResolve) { readyResolve(false); readyResolve = null; }
    };
    readyPromise = new Promise(res => { readyResolve = res; });
    // Both engines get both bases: v1 reads `pyodideBase`, v2 reads
    // `staticBase` (duckdb-wasm, the extension mirror and the Rust parser all
    // live under it). Neither is a CDN — self-hosting is not optional here.
    worker.postMessage({ type: 'explore:init', pyodideBase: PYODIDE_BASE,
      staticBase: STATIC_BASE, catalog: DATA });
    return worker;
  }

  function workerReady() {
    ensureWorker();
    return readyPromise || Promise.resolve(true);
  }

  function onWorkerMessage(e) {
    const msg = e.data || {};
    if (msg.type === 'progress') {
      // The worker's counters are per message (one file each); while the
      // page drives an index run its own "i of N" owns the bar and the
      // worker only contributes the stage text.
      const d = state.indexDriver;
      // A partition served from the cache (server index or a warm folder) is
      // REGISTERED, not parsed — "Parsing" there was a lie.
      const verb = (d && d.cached) || state.source === 'server' ? t('Connecting ') : t('Parsing ');
      if (d) {
        if (msg.msg) setProgress(d.i, d.n, verb + msg.msg);
        return;
      }
      // Outside a file loop the worker's message is a whole phrase
      // ("Loading the query engine") — no verb prefix.
      setProgress(msg.done, msg.total, msg.msg || t('Indexing…'));
      return;
    }
    if (msg.type === 'warning') {
      setSummary(msg.msg);
      return;
    }
    if (msg.type === 'explore:ready') {
      if (readyResolve) { readyResolve(true); readyResolve = null; }
      return;
    }
    // An 'explore:error' carries the id of whatever message failed. Resolving
    // (not rejecting) keeps every caller on the single `res.error` path.
    if (msg.type === 'explore:error') {
      if (readyResolve) { readyResolve(false); readyResolve = null; }
      progressWrap.hidden = true;
      indexBtn.disabled = !state.dirHandle;
      if (msg.id != null && pending.has(msg.id)) {
        const p = pending.get(msg.id);
        pending.delete(msg.id);
        p.resolve({ error: msg.message || t('Engine error') });
      } else {
        setSummary(t('Engine error: {p1}', {p1: (msg.message || '')}));
      }
      return;
    }
    if (msg.id != null && pending.has(msg.id)) {
      const { resolve } = pending.get(msg.id);
      pending.delete(msg.id);
      resolve(msg);
      return;
    }
    if (msg.type === 'explore:indexed') applyIndexed(msg);
  }

  // 'explore:indexed' carries {indexed, errors, tables:[...], stats:{...}} —
  // the header line is built from `stats` (counts), never from `tables`
  // (an array), and the freshness date from the mtimes the page itself read.
  function applyIndexed(msg) {
    state.indexed = true;
    state.lastKill = null;
    state.serverStale = false;
    // The index generation — see state.indexGen. Everything derived from what
    // the engine holds (today: the Grand-Total list of the value lookup) is
    // keyed by it, so a re-index can never be answered from stale numbers.
    state.indexGen += 1;
    state.gtCache = null;
    // Read by tests/chrome/smoke_explore_chrome.py — the engine-2 assertions
    // (Parquet cache mode, peak wasm bytes) have no DOM of their own, and a
    // number that only exists in a console line is a number nothing can gate on.
    try {
      window.__opExploreIndexed = {
        engine: ENGINE, stats: msg.stats || {}, cache: msg.cache || null,
        cached: msg.cached || 0, peakWasmBytes: msg.peakWasmBytes || 0,
        timings: msg.spent || null,
      };
    } catch (e) { /* no window: nothing depends on this */ }
    state.workerTables = msg.tables || [];
    // The chat's coverage pill stands on this and on `workerTables`.
    state.indexStats = msg.stats || {};
    state.semantics = msg.semantics || null;
    progressWrap.hidden = true;
    // "Index" is a FOLDER control: with no folder connected (an
    // index loaded from the server) there is nothing for it to re-read.
    indexBtn.disabled = !state.dirHandle;
    const st = msg.stats || {};
    let text = tn(st.tables || 0, '{n} table|{n} tables')
      + ' · ' + tn(st.partitions || 0, '{n} partition|{n} partitions');
    if (state.maxMtime) text += t(' · data as of {p1} {ctx:2}', {p1: fmtDate(state.maxMtime)});
    // Phase 9.4: an index nobody on this machine built must SAY so — the
    // summary is the one line an operator reads before trusting a number.
    if (state.source === 'server') {
      text += t(' · index from server{p1}{p2}', {p1: (state.serverRun != null ? t(' (run {p1})', {p1: state.serverRun}) : ''), p2: (state.exec === 'server' ? t(' · calculated by server') : '')});
    }
    if (msg.cached) text += t(' · from cache: {p1}', {p1: msg.cached});
    // Where the partition cache actually lives. `opfs` is the intended path
    // (DuckDB writing straight into origin-private storage); anything else is
    // a fallback the operator deserves to see rather than discover as a slow
    // re-index — see docs/ENGINE2.md.
    if (msg.cache && msg.cache.mode && msg.cache.mode !== 'opfs') {
      text += t(' · cache: {p1}', {p1: msg.cache.mode});
    }
    if (msg.spent && msg.spent.total > 0) {
      const s = msg.spent;
      const sec = ms => t('{p1} s', {p1: (ms / 1000).toFixed(ms >= 10000 ? 0 : 1)});
      // A list of whole «label value» items joined by punctuation, not one
      // sentence sliced into ", cache …" fragments: the optional parts have to
      // survive translation on their own.
      const parts = [t('read {p1}', {p1: sec(s.read)}),
                     t('parse {p1}', {p1: sec(s.parse + s.coerce)}),
                     t('load {p1}', {p1: sec(s.register + s.view)})];
      if (s.cache_read) parts.push(t('cache {p1}', {p1: sec(s.cache_read)}));
      if (s.semantics) parts.push(t('metrics {p1}', {p1: sec(s.semantics)}));
      text += t(' · time {p1} ({p2})', {p1: sec(s.total), p2: parts.join(', ')});
    }
    const errs = msg.errors || [];
    if (errs.length) text += t(' · not read: {p1}', {p1: errs.length});
    // Storage footprint, appended asynchronously: the partition cache, the
    // materialised views and the folder handles all live in this origin's
    // storage, and Chrome evicts ALL of it under disk pressure unless the
    // site is persistent — so say how much is used, and ask to persist.
    const text0 = text;
    storageNote().then(n => { if (n && summaryEl.textContent === text0) summaryEl.textContent = text0 + n; });
    if (state.unmatched && state.unmatched.length) {
      text += t(' · not in catalogue: {p1}', {p1: state.unmatched.length});
    }
    const ready = readySemantics();
    if (ready.length) {
      text += t(' · metrics: {p1}', {p1: ready.map(g => g.generator + ' (' + g.kpis.filter(k => k.ready).length + ')').join(', ')});
    }
    setSummary(text);
    populateTableSelect();
    renderMetrics();
    renderReports();
  }

  // ── Report strip (screen A) ──────────────────────────────────────
  // One card per generator-backed dashboard the user may run. The card says
  // three things an operator needs before asking anything: which report it
  // is, which generator (and version) backs it, and whether its metrics can
  // answer over the folder as it stands right now. Clicking one PINS its
  // generator — `pickGenerator` then stops guessing from the question's
  // words. Nothing here runs a query; it is context, not a control panel.
  function reportSemState(r) {
    const ready = readySemantics().find(g => String(g.generator_id) === String(r.generator_id));
    if (ready) {
      let text = tn((ready.kpis || []).filter(k => k.ready).length, '{n} metric|{n} metrics');
      // Columns the engine could not find in the partitions — the honest
      // reason a per-client answer comes back empty (prod SL 2026-08-23).
      const ub = ready.unbound_columns || {};
      const missing = Object.keys(ub).flatMap(tv => (ub[tv] || []).map(c => tv + '.' + c));
      if (missing.length) text += t(' · no columns: {p1}{p2}', {p1: missing.slice(0, 4).join(', '), p2: (missing.length > 4 ? '…' : '')});
      // Which version of each platform dictionary answered (ADR-16). An
      // analyst who has just corrected a mapping needs to see «v7» here to
      // know whether the chat has caught up — without opening "Reference tables".
      const refs = ready.references || [];
      if (refs.length) {
        text += ' · ' + refs.map(r => t('reference table {p1} v{p2}', {p1: r.slug, p2: r.version})).join(', ');
      }
      return text;
    }
    const known = (DATA.semantics || []).some(g => String(g.generator_id) === String(r.generator_id));
    return known ? t('semantics available — indexing needed') : t('no semantics');
  }

  function folderStateText(r) {
    const active = state.reportGen != null && String(state.reportGen) === String(r.generator_id);
    // Phase 9.4: a report answered from its published index has no folder at
    // all — saying "folder not connected" there would read as "broken".
    if (active && state.source === 'server' && state.indexed) {
      return t('index from the server · {p1}', {p1: nOf(state.serverParts || 0, [t('partition'), t('partitions'), t('partitions {ctx:2}')])});
    }
    if (active && state.dirHandle) {
      return state.indexed
        ? t('indexed folder \'{p1}»', {p1: state.dirHandle.name})
        : t('connected folder \'{p1}»', {p1: state.dirHandle.name});
    }
    const name = state.foldersByGen[String(r.generator_id)];
    if (name) return t('folder "{p1}"', {p1: name});
    const man = manifestOf(r);
    if (man) return t('index on server');
    return t('index not published');
  }

  // The card's preview — the same scaled live render the home grid shows,
  // gated the same way (≤3 loading at once, only when visible, heavy
  // reports get a glyph instead). A report is recognised by its face, not
  // by its title alone.
  const PREVIEW_MAX = 1500000;
  const previewQueue = []; let previewInflight = 0;
  function pumpPreviews() {
    while (previewInflight < 3 && previewQueue.length) {
      const f = previewQueue.shift();
      if (!f.dataset.src || f.src) continue;
      previewInflight++;
      let done = false;
      const release = () => { if (done) return; done = true; previewInflight--; pumpPreviews(); };
      f.addEventListener('load', () => { f.style.opacity = '1'; release(); }, { once: true });
      f.addEventListener('error', release, { once: true });
      setTimeout(release, 20000);
      f.src = f.dataset.src;
    }
  }
  let previewObs = null;
  function reportPreview(r) {
    const box = el('span', { class: 'ex-report-pv' });
    if (!r.dashboard_id || (r.size || 0) > PREVIEW_MAX || typeof IntersectionObserver === 'undefined') {
      box.classList.add('is-glyph');
      return box;
    }
    const f = el('iframe', { class: 'ex-report-frame', tabindex: '-1', scrolling: 'no', 'aria-hidden': 'true' });
    f.dataset.src = '/render/' + encodeURIComponent(r.dashboard_id) + '/';
    box.appendChild(f);
    if (!previewObs) {
      previewObs = new IntersectionObserver(entries => {
        entries.forEach(en => {
          if (!en.isIntersecting) return;
          const fr = en.target.querySelector('.ex-report-frame');
          const w = en.target.offsetWidth || 236;
          if (fr) { fr.style.transform = 'scale(' + (w / 1280) + ')'; if (fr.dataset.src && !fr.src) previewQueue.push(fr); }
          previewObs.unobserve(en.target);
        });
        pumpPreviews();
      }, { rootMargin: '200px' });
    }
    previewObs.observe(box);
    return box;
  }
  // Cards are built ONCE and updated in place: rebuilding them on every
  // state change killed the preview iframes before `load` fired, which
  // leaked the ≤3 loading slots and left every preview grey for good.
  const cardByGen = new Map();
  function buildReportCard(r) {
    const card = el('button', { type: 'button', class: 'ex-report-card',
      'data-generator-id': String(r.generator_id),
      'data-dashboard-id': String(r.dashboard_id) });
    card.appendChild(reportPreview(r));
    card.appendChild(el('span', { class: 'ex-report-title', text: r.title || '' }));
    card.appendChild(el('span', { class: 'ex-report-gen',
      text: (r.generator_name || '') + (r.version ? ' v' + r.version : '') }));
    card.appendChild(el('span', { class: 'ex-report-state' }));
    // The server line and the load link are attached only while they apply
    // (tests and the harness count `.ex-report-load` as "offered").
    card._srv = el('span', { class: 'ex-report-server' });
    // A <span>, not a <button>: the card itself IS a button and nesting
    // one inside it is invalid HTML the browser is free to reparent.
    const load = el('span', { class: 'ex-report-load', role: 'button', tabindex: '0',
      text: t('Load index') });
    card._load = load;
    const go = (ev) => {
      if (ev && typeof ev.stopPropagation === 'function') ev.stopPropagation();
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      loadServerIndex(r);
    };
    load.addEventListener('click', go);
    load.addEventListener('keydown', (ev) => { if (ev && (ev.key === 'Enter' || ev.key === ' ')) go(ev); });
    card.addEventListener('click', () => {
      if (String(state.reportGen) === String(r.generator_id)) {
        state.reportGen = null; // un-pin: the question decides again
        renderReports();
        return;
      }
      state.reportGen = r.generator_id;
      renderReports();
      // The report's index (server, all folders) is the data here; a stored
      // folder handle is only consulted in the folder service mode.
      if (FOLDER_MODE) switchReportFolder(r.generator_id);
      else if (!state.indexed && !state.indexing) maybeServerAutoIndex();
    });
    return card;
  }
  function renderReports() {
    if (typeof syncLoadIndexBtn === 'function') syncLoadIndexBtn();
    if (!reportStrip) return;
    REPORTS.forEach(r => {
      const key = String(r.generator_id);
      let card = cardByGen.get(key);
      if (!card) { card = buildReportCard(r); cardByGen.set(key, card); reportStrip.appendChild(card); }
      card.classList.toggle('active', String(state.reportGen) === key);
      card.querySelector('.ex-report-state').textContent = reportSemState(r) + ' · ' + folderStateText(r);
      const man = manifestOf(r);
      const loadedHere = state.source === 'server' && state.indexed && String(state.reportGen) === key;
      const srv = card._srv, load = card._load;
      if (man && !loadedHere) {
        const parts = (man.files || []).length;
        const when = (man.files || []).reduce((m, f) => Math.max(m, f.mtime_ms || 0), 0);
        srv.textContent = t('index on the server · {p1}{p2}', {p1: nOf(parts, [t('partition'), t('partitions'), t('partitions {ctx:2}')]), p2: (when ? t(' · data as of {p1} {ctx:2}', {p1: fmtDayMonth(when)}) : '')});
        if (!srv.parentElement) card.appendChild(srv);
        if (!load.parentElement) card.appendChild(load);
      } else {
        if (srv.parentElement) srv.remove();
        if (load.parentElement) load.remove();
      }
    });
  }

  // ── Metrics panel (ADR-13): the deterministic path ───────────────
  // Every ready KPI of every ready generator is a button. Its form is the
  // KPI's own dims + the time dims as period pickers + top-N; "Calculate"
  // turns the form into an intent, the worker builds the SQL from the frozen
  // definition, the page shows it and runs it. No model anywhere here — a
  // wrong answer on this path is a wrong definition, never a guess.
  const TIME_DIMS = { year: t('Year'), month: t('Month'), week: t('Week'), day: t('Day') };
  // Rebuilt by renderMetrics; askIntent's form path looks a KPI up here to
  // open the very same panel the operator would have clicked.
  const metricEntries = [];

  // The chip row itself, factored out of `renderMetrics` so the chat view can
  // put the SAME buttons under its input without duplicating what a metric
  // chip looks like or which KPIs are offered. `onPick` receives
  // `{btn, gen, kpi}`; the sidebar passes `selectMetric`, the chat passes its
  // own handler. Returns the entries it built — the sidebar keeps them in
  // `metricEntries` (that array is `applyFormIntent`'s lookup table and must
  // describe the SIDEBAR's buttons, never the chat's).
  function metricChips(container, onPick) {
    const out = [];
    if (!container) return out;
    container.textContent = '';
    const ready = readySemantics();
    ready.forEach(g => (g.kpis || []).filter(k => k.ready).forEach(k => {
      const btn = el('button', { type: 'button', class: 'ex-recipe-btn', 'data-kpi': k.key,
        title: (k.definition || '') + (ready.length > 1 ? ' (' + g.generator + ')' : ''),
        text: k.short || k.name || k.key });
      const entry = { btn: btn, gen: g, kpi: k };
      out.push(entry);
      btn.addEventListener('click', () => onPick(entry));
      container.appendChild(btn);
    }));
    return out;
  }

  function renderMetrics() {
    if (!metricList) return;
    metricForm.textContent = '';
    metricEntries.length = 0;
    const ready = readySemantics();
    const any = ready.some(g => (g.kpis || []).some(k => k.ready));
    if (metricHead) metricHead.hidden = !any;
    metricChips(metricList, selectMetric).forEach(e => metricEntries.push(e));
    emit('metrics', { ready: ready });
  }

  // One way in for both the click and the form-matched question — a
  // synthetic click() would be a different code path in a real browser.
  function selectMetric(entry) {
    Array.from(metricList.children).forEach(b => b.classList.toggle('active', b === entry.btn));
    renderMetricForm(entry.gen, entry.kpi);
  }

  // "Owner: <owner> · since <date>" / "Owner: <owner>" / "Since <date>" — only
  // the parts that are actually set, textContent-only (owner is free text:
  // a role or position, never assumed to be a person's name).
  function metricOwnerLine(kpi) {
    const owner = kpi && kpi.owner;
    const approvedAt = kpi && kpi.approved_at;
    const parts = [];
    if (owner) parts.push(t('Owner: {p1}', {p1: owner}));
    if (approvedAt) parts.push(t('from {p1}', {p1: approvedAt}));
    return parts.join(' · ');
  }

  function renderMetricForm(gen, kpi) {
    metricForm.textContent = '';
    const dims = kpi.dims || [];
    const groupDims = dims.filter(d => !(d in TIME_DIMS));
    const timeDims = dims.filter(d => d in TIME_DIMS);
    const wrap = el('div', { class: 'ex-metric-wrap' });
    wrap.appendChild(el('div', { class: 'mono ex-metric-def', text: kpi.definition || kpi.name || '' }));
    // ADR: owner/approved_at are both optional (a KPI without either shows
    // no subtitle at all, not an empty line) — free-text role/position, not
    // necessarily a person's name, so this is never treated as a name field.
    const ownerLine = metricOwnerLine(kpi);
    if (ownerLine) wrap.appendChild(el('div', { class: 'ex-metric-owner', text: ownerLine }));
    if (groupDims.length) {
      wrap.appendChild(el('label', { text: t('By dimension') }));
      const box = el('div', { class: 'ex-metric-dims' });
      groupDims.forEach(d => {
        const id = 'ex-mdim-' + d;
        const cb = el('input', { type: 'checkbox', id: id, 'data-dim': d });
        box.appendChild(el('label', { class: 'ex-metric-dim', for: id }, [cb, el('span', { text: ' ' + d })]));
      });
      wrap.appendChild(box);
    }
    const periodInputs = {};
    timeDims.forEach(d => {
      wrap.appendChild(el('label', { text: TIME_DIMS[d] }));
      let input;
      if (d === 'month') {
        input = el('select', { class: 'input sm', 'data-time': d });
        input.appendChild(el('option', { value: '', text: t('— all —') }));
        [t('January'), t('February'), t('March'), t('April'), t('May'), t('June'), t('July'), t('August'), t('September'), t('October'), t('November'), t('December')]
          .forEach((m, i) => input.appendChild(el('option', { value: String(i + 1), text: m })));
      } else {
        input = el('input', { class: 'input sm', type: 'number', 'data-time': d, placeholder: d === 'year' ? String(new Date().getFullYear()) : t('— all —') });
      }
      periodInputs[d] = input;
      wrap.appendChild(input);
    });
    wrap.appendChild(el('label', { text: t('Show') }));
    const order = el('select', { class: 'input sm', 'data-metric-order': '1' });
    [['desc', t('largest first')], ['asc', t('smallest first')]].forEach(([v, tv]) => order.appendChild(el('option', { value: v, text: tv })));
    wrap.appendChild(order);
    const limit = el('select', { class: 'input sm', 'data-metric-limit': '1' });
    [1, 5, 10, 100, 1000].forEach(n => limit.appendChild(el('option', { value: String(n), text: n === 1 ? t('first only') : t('first {p1}', {p1: n}) })));
    limit.value = '10';
    wrap.appendChild(limit);
    const run = el('button', { type: 'button', class: 'btn sm primary', 'data-metric-run': '1', text: t('Calculate') });
    run.addEventListener('click', () => runMetric(gen, kpi, wrap, order, limit));
    wrap.appendChild(run);
    wrap.appendChild(el('div', { class: 'mono ex-metric-status', 'data-metric-status': '1' }));
    metricForm.appendChild(wrap);
  }

  async function runMetric(gen, kpi, wrap, order, limit) {
    const status = wrap.querySelector('[data-metric-status]');
    const dims = Array.from(wrap.querySelectorAll('input[type=checkbox][data-dim]')).filter(c => c.checked).map(c => c.getAttribute('data-dim'));
    const filters = [];
    Array.from(wrap.querySelectorAll('[data-time]')).forEach(inp => {
      const v = String(inp.value || '').trim();
      if (v) filters.push({ dim: inp.getAttribute('data-time'), op: '=', value: Number(v) });
    });
    const intent = { kpi: kpi.key, generator_id: gen.generator_id, dims: dims, filters: filters,
      order: { by: 'value', dir: order.value === 'asc' ? 'asc' : 'desc' }, limit: Number(limit.value) || 10 };
    status.textContent = t('Building the query…');
    const out = await send('explore:intent', { intent: intent }, HARD_QUERY_TIMEOUT_MS);
    if (out && out.sql) emit('stage', { stage: 'sql' });
    if (!out || out.error) { status.textContent = (out && out.error) || t('Failed to build the query.'); return; }
    sqlBox.value = out.sql || '';
    status.textContent = t('Calculating using the definition "{p1}"…', {p1: (kpi.name || kpi.key)});
    // What the thread record's "question" line will say for a form-driven run:
    // nobody typed anything, so the metric's own label is the honest question.
    state.askQuestion = t('Metric "{p1}"', {p1: (kpi.short || kpi.name || kpi.key)});
    // Deterministic path: the SQL came from the frozen definition, so the SQL
    // row stays closed — there is nothing for the operator to check.
    setSqlRowOpen(false);
    primeAnswer(firstClause(intentStatus(out.kpi || kpi, gen, intent)),
                Object.assign({}, kpi, out.kpi || {}), intent);
    await runQuery();
    status.textContent = t('Done — SQL is in the editor, result below.');
  }

  // Generators whose semantics (ADR-13) have at least one KPI standing on
  // the tables indexed right now — the only ones "Ask" may route to.
  function readySemantics() {
    const gens = (state.semantics && state.semantics.generators) || [];
    return gens.filter(g => (g.kpis || []).some(k => k.ready));
  }

  // DuckDB's blocking `execute()` cannot be pre-empted from inside Pyodide —
  // there are no pthreads there, so _explore.py's own `threading.Timer` watchdog
  // is a documented no-op in the browser (it only fires for the CPython test
  // suite). Terminating the WORKER is the one mechanism that actually stops a
  // runaway query in a no-thread wasm build: it aborts whatever the worker's
  // single thread was doing, instantly. That is why `runQuery` races `send`
  // against this timeout instead of trusting the Python-side deadline alone.
  //
  // Ordering contract: the WORKER's own interrupt (QUERY_TIMEOUT_S = 30 s in
  // static/py/_explore.py, and the mirrored deadline in
  // static/js/explore2/worker.js) MUST fire first and answer with its own
  // friendly "stopped: took longer than 30 s" message over a live worker — that path
  // keeps the index intact (see applyIndexed/killWorker below). This page-side
  // timer is the LAST RESORT for a worker that is truly wedged and never
  // answers at all; it pays for that margin over the worker's 30 s with a
  // worse outcome (the whole worker — and the index with it — is killed), so
  // it must stay comfortably above 30 s. 45 s = 30 s worker interrupt + 15 s
  // margin for Pyodide/DuckDB interrupt latency. `window.__opExploreHardTimeoutMs`
  // lets tests override this without waiting out a real 45 s timer.
  const HARD_QUERY_TIMEOUT_MS = (function () {
    try {
      const v = window.__opExploreHardTimeoutMs;
      if (typeof v === 'number' && v > 0) return v;
    } catch (e) { /* no window */ }
    // 120 s: a Pyodide/DuckDB call is synchronous, so the worker's own
    // interrupt cannot fire mid-query — this timer is the only exit, and it
    // costs the index (30–40 s to rebuild on an office laptop). Give a slow
    // machine room to finish rather than killing a query that would have.
    return 120000;
  })();

  function killWorker(reason) {
    if (worker) { try { worker.terminate(); } catch (e) { /* already gone */ } }
    worker = null;
    readyPromise = null;
    readyResolve = null;
    pending.forEach(p => p.resolve({ error: reason }));
    pending.clear();
    // The terminated worker's DuckDB connection and every registered frame
    // are gone with it — the page must not claim the folder is still
    // indexed, or the next query would silently run over an empty engine.
    state.indexed = false;
    state.indexing = false;
    state.lastKill = reason || t('engine reset');
    if (indexBtn) indexBtn.disabled = !state.dirHandle;
    renderReports();
  }

  // ── ADR-14b: the server as a query transport ─────────────────────────
  // When a report's published lake answers on the server, these three ops
  // never wake the worker: the endpoint returns the SAME dict the worker
  // would post, so everything above `send()` stays engine-blind. Flag read
  // dynamically — the harness flips it per scenario.
  function lakeExecOn() { return root.dataset.lakeExec === '1'; }
  const SERVER_OPS = { 'explore:intent': 'intent', 'explore:sql': 'sql', 'explore:export': 'export' };
  async function serverSend(type, payload) {
    const body = { op: SERVER_OPS[type] };
    if (body.op === 'intent') body.intent = (payload && payload.intent) || {};
    else {
      body.sql = String((payload && payload.sql) || '');
      if (body.op === 'sql') body.limit = (payload && payload.limit) || 1000;
      else body.name = (payload && payload.name) || 'explore.xlsx';
    }
    // Server-side answer record (explore_answers): only the ONE `explore:sql`
    // call each answer-producing function (runQuery/trendCard/whyCard's
    // headline whyRun) marks `final: true` on carries the question through.
    // Every other `explore:sql`/`explore:intent` call in this file (building
    // SQL for display, a why-card's prev-period/concentration sub-queries,
    // grandTotals' background loop) sends neither — the server records
    // nothing for those, which is what keeps this at one row per answer
    // instead of one per DuckDB round-trip.
    if (body.op === 'sql' && payload && payload.final) {
      body.final = true;
      body.question = String(payload.question || '').slice(0, 2000);
    }
    try {
      const r = await fetch(lakeUrl(state.execDash, '/exec'), { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (body.op === 'export') {
        if (!r.ok) return { error: t('Export failed ({p1}).', {p1: r.status}) };
        return { name: body.name, buf: await r.arrayBuffer() };
      }
      const out = await r.json().catch(() => null);
      if (!r.ok || !out) {
        const m = out && out.detail && (out.detail.message || out.detail);
        return { error: (typeof m === 'string' && m) || (t('The server did not respond to the request ({p1}).', {p1: r.status})) };
      }
      return out;
    } catch (e) {
      return { error: t('Server unavailable — retry or connect a folder.') };
    }
  }

  function send(type, payload, timeoutMs, transfer) {
    if (state.exec === 'server' && SERVER_OPS[type]) return serverSend(type, payload);
    const id = ++msgSeq;
    return workerReady().then(() => new Promise((resolve, reject) => {
      let timer = null;
      pending.set(id, {
        resolve: (msg) => { if (timer) clearTimeout(timer); resolve(msg); },
        reject,
      });
      if (timeoutMs) {
        timer = setTimeout(() => {
          if (!pending.has(id)) return; // already resolved, timer was stale
          pending.delete(id);
          // Honest about what actually happened: the WORKER's own 30 s
          // interrupt (which would have kept the index) never answered
          // either, so this is a wedged engine, not a missing folder — only
          // say "connect the folder" when the folder connection itself is
          // gone.
          const secs = Math.round(timeoutMs / 1000);
          // Two whole sentences rather than one with a "connect the folder and "
          // clause spliced in: the clause is not a translatable unit.
          const msg = state.dirHandle
            ? t('The request did not finish within {p1} s and the engine did not respond — the index has been reset, load it again and narrow the period or add a partition filter.', {p1: secs})
            : t('The request did not finish within {p1} s and the engine did not respond — the index has been reset, connect the folder and load it again and narrow the period or add a partition filter.', {p1: secs});
          killWorker(msg);
          resolve({ error: msg });
        }, timeoutMs);
      }
      if (transfer && transfer.length) {
        worker.postMessage(Object.assign({ type: type, id: id }, payload || {}), transfer);
        return;
      }
      worker.postMessage(Object.assign({ type: type, id: id }, payload || {}));
    }));
  }

  function setProgress(done, total, msg) {
    progressWrap.hidden = false; openSources();
    // A folder of ONE large workbook sat at 0 % for its whole parse — the
    // item in flight counts as half done, and the fill pulses while busy so
    // the bar reads "working", not "stuck".
    const busy = total && done < total;
    const pct = total ? Math.round(((done + (busy ? 0.5 : 0)) / total) * 100) : 0;
    progressFill.style.width = pct + '%';
    progressFill.classList.toggle('busy', !!busy);
    progressMsg.textContent = (msg || '') + ' (' + done + '/' + total + ')';
  }
  function setSummary(text) { summaryEl.textContent = text || ''; }
  // "storage 1.2 GB of 30 GB · persistent" — what the browser holds for
  // this site and whether it promised not to evict it.
  async function storageNote() {
    try {
      if (!navigator.storage || !navigator.storage.estimate) return '';
      const est = await navigator.storage.estimate();
      const gb = b => t('{p1} GB', {p1: (b / 1073741824).toFixed(b >= 10737418240 ? 0 : 1)});
      let persisted = null;
      if (navigator.storage.persisted) persisted = await navigator.storage.persisted();
      if (persisted === false && navigator.storage.persist) persisted = await navigator.storage.persist();
      return t(' · storage {p1} of {p2}{p3}', {p1: gb(est.usage || 0), p2: gb(est.quota || 0), p3: (persisted === true ? t(' · protected from cleanup') : persisted === false ? t(' · may be cleared by the browser') : '')});
    } catch (e) { return ''; }
  }
  // Russian-style plural: 1 / 2–4 / 5+ forms (one table / two tables / five tables)
  function plural(n, forms) {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return forms[2];
    if (b > 1 && b < 5) return forms[1];
    if (b === 1) return forms[0];
    return forms[2];
  }
  function nOf(n, forms) { return n + ' ' + plural(n, forms); }
  function fmtDate(ms) {
    const d = new Date(ms);
    if (isNaN(d.getTime())) return '';
    return OP_I18N.fmtDate(d);
  }
  // DD.MM — the report card has room for the day and the month, not a year.
  function fmtDayMonth(ms) {
    const d = new Date(ms);
    if (isNaN(d.getTime())) return '';
    const two = n => (n < 10 ? '0' : '') + n;
    return two(d.getDate()) + '.' + two(d.getMonth() + 1);
  }

  // ══════════════════════════════════════════════════════════════════
  // TEST-ONLY hook. `showDirectoryPicker()` opens a native OS dialog that no
  // automation (CDP included) can drive, so the headless-Chrome end-to-end
  // harness (tests/chrome/smoke_explore_chrome.py) substitutes the folder:
  // set `window.__opExploreTestFiles = [{name, buf: ArrayBuffer}, ...]`
  // BEFORE clicking "Connect folder" and the page reads that array instead
  // of asking for a directory handle. Gated on localhost so it is inert in
  // any real deployment; nothing else in the page depends on it.
  // ══════════════════════════════════════════════════════════════════
  function testFiles() {
    if (location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') return null;
    const f = window.__opExploreTestFiles;
    return (Array.isArray(f) && f.length) ? f : null;
  }

  // ── connect ──────────────────────────────────────────────────────
  async function connectFolder() {
    const tf = testFiles();
    if (tf) { state.dirHandle = { name: 'test', __test: true }; setFolderReady('test'); return; }
    if (typeof window.showDirectoryPicker !== 'function') {
      statusEl.textContent = t('The browser doesn\'t support folder access.');
      return;
    }
    let h;
    try { h = await window.showDirectoryPicker({ mode: 'read' }); }
    catch (e) { return; } // user cancelled
    state.dirHandle = h;
    state.indexed = false;
    if (state.reportGen != null) state.foldersByGen[String(state.reportGen)] = h.name;
    try {
      await idbPutHandle(h, handleKeyFor(state.reportGen));
      if (state.reportGen != null) await idbPutHandle(h); // legacy key too
    } catch (e) { /* best-effort */ }
    setFolderReady(h.name);
  }

  // The report card changed: drop the old folder, pick up this report's own
  // one (auto-index when the permission is still granted — the usual case
  // within a session), or say honestly that none is connected yet.
  let switchSeq = 0;
  async function switchReportFolder(gen) {
    const my = ++switchSeq;
    let h = null;
    try { h = await idbGetHandle(handleKeyFor(gen)); } catch (e) { h = null; }
    if (my !== switchSeq) return;
    // No folder of its own yet: keep whatever is connected (one folder can
    // legitimately serve several reports) and say so on the card.
    if (!h || (state.dirHandle && h.name === state.dirHandle.name && !state.dirHandle.__test
               && typeof h.isSameEntry === 'function' && await h.isSameEntry(state.dirHandle).catch(() => false))) {
      if (!h && !state.dirHandle) statusEl.textContent = t('This report\'s index is not loaded');
      renderReports();
      return;
    }
    state.indexed = false;
    state.dirHandle = h;
    reconnectBtn.hidden = true;
    indexBtn.disabled = true;
    state.foldersByGen[String(gen)] = h.name;
    let perm = 'prompt';
    try { perm = await h.queryPermission({ mode: 'read' }); } catch (e) { perm = 'prompt'; }
    if (my !== switchSeq) return;
    if (perm === 'granted') { setFolderReady(h.name); runIndex(); return; }
    reconnectBtn.hidden = false;
    statusEl.textContent = t('Folder "{p1}" was connected before — permission needed', {p1: h.name});
    renderReports();
  }

  async function reconnectFolder() {
    if (!state.dirHandle) return;
    try {
      const perm = await state.dirHandle.requestPermission({ mode: 'read' });
      if (perm === 'granted') { reconnectBtn.hidden = true; setFolderReady(state.dirHandle.name); }
    } catch (e) { /* stays disconnected */ }
  }

  function setFolderReady(name) {
    statusEl.textContent = t('Folder with files "{p1}" connected', {p1: name});
    indexBtn.disabled = false;
    renderReports();
  }

  async function initFromStoredHandle() {
    // The viewer's pane answers over the report's index — built by the run
    // over ALL its folders. A folder handle left from an earlier visit must
    // not pull the pane into a two-minute re-index of ONE folder.
    if (root.dataset.embed === '1' || !FOLDER_MODE) { maybeServerAutoIndex(); return; }
    let all = {};
    // A browser that refuses IndexedDB has no stored folder — but the server
    // index still works, so this must fall through, not return.
    try { all = await idbAllHandles(); } catch (e) { all = {}; }
    REPORTS.forEach(r => {
      const h = all[handleKeyFor(r.generator_id)];
      if (h && h.name) state.foldersByGen[String(r.generator_id)] = h.name;
    });
    // The chosen report's own folder first (embed mode pins one via
    // `?report=`), else the legacy single folder.
    const h = (state.reportGen != null && all[handleKeyFor(state.reportGen)]) || all[HANDLE_KEY] || null;
    renderReports();
    if (!h) { maybeServerAutoIndex(); return; }
    state.dirHandle = h;
    try {
      const perm = await h.queryPermission({ mode: 'read' });
      if (perm === 'granted') {
        setFolderReady(h.name);
        // Inside the viewer's "Chat" pane nobody will click "Index":
        // the report's folder is known and readable — index it now. The
        // folder beats the server index here: it is the fresher of the two.
        if (root.dataset.embed === '1') runIndex();
        return;
      }
    } catch (e) { /* fall through to reconnect */ }
    reconnectBtn.hidden = false; openSources();
    statusEl.textContent = t('The folder was connected before — permission needed');
    // The pane cannot ask for that permission — the server index can answer
    // without it.
    maybeServerAutoIndex();
  }

  // ═════════════════════════════════════════════════════════════════
  // Server index (phase 9.4) — answer over a report WITHOUT its folder
  // ═════════════════════════════════════════════════════════════════
  // The report's own run published its Parquet partitions to
  // `/api/dashboards/{id}/lake` (phase 9.1–9.3). Anyone with access to the
  // dashboard may pull them down and import them into this browser's
  // partition cache, which means the FIRST question of a user who has never
  // seen the share folder costs a download, not a full re-parse of every
  // workbook — and a user with no access to the share can ask at all.
  //
  // Discipline: the folder always wins when it is readable (it is fresher
  // than any published run), the parser version has to match exactly (an
  // index built by another engine version coerces types differently — see
  // _explore.PARSER_VERSION), and `/` never pays for any of this: the
  // manifests are fetched from THIS page, after first paint.
  const SERVER_FETCH_PARALLEL = 8;

  function lakeUrl(dashId, tail) {
    return '/api/dashboards/' + encodeURIComponent(String(dashId)) + '/lake' + (tail || '');
  }

  // 404 (no index published) is the normal answer, not an error — it is what
  // every report looks like until its first run with a semantics bundle.
  async function fetchManifest(dashId) {
    try {
      const r = await fetch(lakeUrl(dashId, '/manifest'), { headers: { Accept: 'application/json' } });
      if (!r.ok) return null;
      const m = await r.json();
      return (m && Array.isArray(m.files) && m.files.length) ? m : null;
    } catch (e) { return null; }
  }

  function manifestOf(r) {
    const m = r ? state.serverManifests[String(r.generator_id)] : null;
    return m || null;   // `false` (asked, none) and undefined (not asked) both read as none
  }

  // One manifest per card, at most SERVER_FETCH_PARALLEL in flight, fired
  // after the page has painted. Nothing here blocks a question.
  async function probeServerIndexes() {
    // `lake` comes from the server snapshot (a stat per card): only reports
    // that HAVE an index are asked for their manifest — no 404 noise.
    const list = REPORTS.filter(r => r && r.dashboard_id != null && r.lake !== false);
    if (!list.length) return;
    let next = 0;
    const pull = async () => {
      while (next < list.length) {
        const r = list[next++];
        const m = await fetchManifest(r.dashboard_id);
        state.serverManifests[String(r.generator_id)] = m || false;
      }
    };
    const lanes = Math.min(SERVER_FETCH_PARALLEL, list.length);
    await Promise.all(Array.from({ length: lanes }, pull));
    renderReports();
  }

  // The engine's own parser version, asked once. An empty probe is the
  // cheapest question that carries it (gen-worker answers `parser_version`
  // on every 'explore:cache_probed').
  async function parserVersion() {
    if (state.parserVersion) return state.parserVersion;
    const probe = await send('explore:cache_probe', { files: [] }, 0);
    state.parserVersion = (probe && probe.parser_version) || null;
    return state.parserVersion;
  }

  function serverStatus(text) {
    statusEl.textContent = text;
    setSummary(text);
  }

  function tableIdByName(name) {
    if (!name) return null;
    const tv = (DATA.tables || []).find(x => x.name === name);
    return tv ? tv.table_id : null;
  }

  let serverLoading = false;

  // ADR-14b: the report's lake answers ON THE SERVER — no engine boot, no
  // partition download. One `status` call builds (or reuses) the server-side
  // instance and returns what `explore:indexed` would have carried; from
  // here on `send()` routes intent/sql/export to `POST …/lake/exec`.
  async function activateServerExec(r) {
    if (serverLoading || !r || r.dashboard_id == null) return false;
    serverLoading = true;
    state.indexing = true;
    try {
      state.reportGen = r.generator_id;
      renderReports();
      progressWrap.hidden = false; openSources();
      setProgress(0, 1, t('Opening the index on the server…'));
      let rr = null, out = null;
      try {
        rr = await fetch(lakeUrl(r.dashboard_id, '/exec'), { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ op: 'status' }) });
        out = await rr.json().catch(() => null);
      } catch (e) { rr = null; }
      if (!rr || rr.status === 404) {
        progressWrap.hidden = true;
        if (rr) state.serverManifests[String(r.generator_id)] = false;
        serverStatus(t('There is no index for this report on the server — connect a folder.'));
        return false;
      }
      if (!rr.ok || !out) {
        progressWrap.hidden = true;
        const m = out && out.detail && (out.detail.message || out.detail);
        serverStatus((typeof m === 'string' && m) || t('The server could not open the index — try again later.'));
        return false;
      }
      const man = out.manifest || {};
      state.serverManifests[String(r.generator_id)] = man;
      const files = man.files || [];
      const byName = new Map();
      files.forEach(f => { if (!byName.has(f.name)) byName.set(f.name, { name: f.name, size: f.size, mtime_ms: f.mtime_ms }); });
      state.lastFiles = Array.from(byName.values());
      state.unmatched = [];
      state.matchedTableIds = Array.from(new Set(files.map(f => tableIdByName(f.table)).filter(Boolean)));
      state.maxMtime = files.reduce((m, f) => Math.max(m, f.mtime_ms || 0), 0);
      state.exec = 'server';
      state.execDash = r.dashboard_id;
      state.source = 'server';
      state.serverRun = man.run_id != null ? man.run_id : null;
      state.serverParts = files.length;
      applyIndexed({ indexed: files.length, cached: 0, errors: out.index_errors || [],
        tables: out.tables || [], stats: out.stats || {},
        semantics: out.semantics || null, cache: null });
      return true;
    } finally {
      serverLoading = false;
      state.indexing = false;
    }
  }

  // The whole "Load index" path: manifest → version gate → download
  // the partitions this browser does NOT already hold → import them → drive
  // the SAME `explore:index` cache path `runIndex` uses for its own hits.
  async function loadServerIndex(r) {
    if (serverLoading || !r || r.dashboard_id == null) return;
    // Server-side execution first: the same lake, zero boot. The download
    // path below stays as the fallback (flag off, or the server refused).
    if (lakeExecOn()) { await activateServerExec(r); return; }
    serverLoading = true;
    state.indexing = true;
    try {
      // Loading a report's index is choosing that report: the question must
      // be answered by ITS generator, not by a word-overlap guess.
      state.reportGen = r.generator_id;
      renderReports();

      let man = manifestOf(r);
      if (!man) {
        man = await fetchManifest(r.dashboard_id);
        state.serverManifests[String(r.generator_id)] = man || false;
        renderReports();
      }
      if (!man) { serverStatus(t('There is no index for this report on the server — connect a folder.')); return; }

      progressWrap.hidden = false; openSources();
      setProgress(0, 1, t('Loading engine…'));
      const ok = await workerReady();
      if (!ok) { progressWrap.hidden = true; return; }

      const pv = await parserVersion();
      if (pv && man.parser_version && String(pv) !== String(man.parser_version)) {
        progressWrap.hidden = true;
        serverStatus(t('The index on the server was built with a different engine version — recalculate the report'));
        return;
      }

      const files = (man.files || []).filter(f => f && f.key);
      if (!files.length) { progressWrap.hidden = true; serverStatus(t('The index on the server is empty — recalculate the report.')); return; }

      const errors = [];
      // What this browser already holds never travels: the probe answers
      // over the SAME metadata the keys were minted from, so a second
      // "Load index" (or a folder index of the same files) is free.
      setProgress(0, files.length, t('Checking the cache…'));
      const probe = await send('explore:cache_probe', {
        files: files.map(f => ({ name: f.name, size: f.size, mtime_ms: f.mtime_ms,
          sheet: f.sheet, header_row: f.header_row || 0 })),
      }, 0);
      const probeKeys = (probe && probe.keys) || [];
      const probeHit = (probe && probe.cached) || [];
      const held = new Set();
      probeKeys.forEach((k, i) => { if (probeHit[i]) held.add(String(k)); });
      const misses = files.filter(f => !held.has(String(f.key)));

      for (let i = 0; i < misses.length; i++) {
        const f = misses[i];
        setProgress(i, misses.length, t('Downloading partitions from the server'));
        let buf = null;
        try {
          const resp = await fetch(lakeUrl(r.dashboard_id, '/parts/' + encodeURIComponent(String(f.key))));
          if (!resp.ok) throw new Error(String(resp.status));
          buf = await resp.arrayBuffer();
        } catch (e) {
          errors.push({ name: f.name, error: t('partition failed to download') });
          continue;
        }
        // Transferred, like every other buffer this page hands the worker:
        // the partition exists in exactly one place at a time.
        const imp = await send('explore:cache_import', { key: String(f.key), buf: buf }, 0, [buf]);
        if (!imp || imp.ok !== true) {
          errors.push({ name: f.name, error: (imp && imp.error) || t('partition rejected') });
        }
      }

      // Synthetic file records: the manifest already carries everything
      // `matchFilesToCatalog` would have derived from a folder scan (table,
      // sheet, partition, header row), and it is the run's own truth — no
      // re-matching against a catalog snapshot that may have moved on.
      const recs = files.map(f => ({
        dir: 0, name: f.name, size: f.size, mtime_ms: f.mtime_ms,
        table: f.table || null, table_id: tableIdByName(f.table),
        sheet: f.sheet || null, header_row: f.header_row || 0,
        partition: f.partition == null ? null : f.partition,
        cached: true, cache_key: String(f.key),
      }));
      // Provenance stands on the FILES' own mtimes, exactly as a folder scan
      // would leave them — one entry per physical workbook, not per sheet.
      const byName = new Map();
      files.forEach(f => { if (!byName.has(f.name)) byName.set(f.name, { name: f.name, size: f.size, mtime_ms: f.mtime_ms }); });
      state.lastFiles = Array.from(byName.values());
      state.unmatched = [];
      state.matchedTableIds = Array.from(new Set(recs.map(x => x.table_id).filter(Boolean)));
      state.maxMtime = files.reduce((m, f) => Math.max(m, f.mtime_ms || 0), 0);

      let indexed = 0, cachedTotal = 0, lastMsg = null;
      const spent = { read: 0, fs_write: 0, parse: 0, coerce: 0, register: 0, view: 0, cache_write: 0, cache_read: 0, semantics: 0 };
      const addSpent = (res) => { const tv = (res && res.timings) || {}; Object.keys(tv).forEach(k => { if (k in spent) spent[k] += tv[k] || 0; }); };
      const tRun = Date.now();
      for (let i = 0; i < recs.length; i++) {
        state.indexDriver = { i: i, n: recs.length, cached: true };
        setProgress(i, recs.length, t('Connecting {p1}', {p1: recs[i].name}));
        const res = await send('explore:index', { files: [recs[i]], final: i === recs.length - 1 }, 0);
        indexed += res.indexed || 0;
        cachedTotal += res.cached || 0;
        (res.errors || []).forEach(e => errors.push(e));
        addSpent(res);
        lastMsg = res;
      }
      state.indexDriver = null;
      setProgress(recs.length, recs.length, t('Done'));
      spent.total = Date.now() - tRun;
      // Set BEFORE applyIndexed: it builds the summary and repaints the
      // cards, and both have to say where these numbers came from.
      state.source = 'server';
      state.serverRun = man.run_id != null ? man.run_id : null;
      state.serverParts = files.length;
      applyIndexed({ indexed: indexed, cached: cachedTotal, errors: errors,
        tables: lastMsg ? lastMsg.tables : [], stats: lastMsg ? lastMsg.stats : {},
        semantics: lastMsg ? lastMsg.semantics : null, cache: lastMsg ? lastMsg.cache : null,
        peakWasmBytes: lastMsg ? lastMsg.peakWasmBytes : 0, spent: spent });
    } finally {
      serverLoading = false;
      state.indexing = false;
    }
  }

  // Inside the viewer's "Chat" pane nobody will click anything: `?report=`
  // names the report, and if its folder is missing (or its handle needs a
  // permission the pane cannot ask for) the server index is the only way the
  // pane answers at all. A readable folder wins — it is fresher.
  // The viewer tells the pane when a run has just published the index.
  if (root.dataset.embed === '1') {
    window.addEventListener('message', (e) => {
      if (e.origin !== location.origin) return;
      const d = e.data;
      if (!d || d.type !== 'op_lake_published') return;
      const rep = REPORTS.find(x => String(x.dashboard_id) === String(root.dataset.report || ''));
      if (!rep) return;
      rep.lake = true;
      delete state.serverManifests[String(rep.generator_id)];
      if (!state.indexed && !state.indexing) { maybeServerAutoIndex(); return; }
      // An index is already loaded: the run just replaced the data, so the
      // one on screen is stale. Do not silently rebuild mid-conversation —
      // say so and let the person refresh when they want to.
      state.serverStale = true;
      renderReports();
    });
  }
  // Which report the page is ABOUT, when it is about one: the viewer's
  // `?report=`, a card pinned by the chat (`#q=` / state.reportGen), or the
  // only report there is. Used to pick a server index up without a click —
  // on /explore as much as in the pane ("it's asking to index again").
  function focusedReport() {
    const dashId = root.dataset.report || '';
    if (dashId) return REPORTS.find(x => String(x.dashboard_id) === String(dashId)) || null;
    if (state.reportGen != null) return REPORTS.find(x => String(x.generator_id) === String(state.reportGen)) || null;
    return null;   // no pin: the cards offer "Load index", nothing loads by itself
  }
  async function maybeServerAutoIndex() {
    const rep = focusedReport();
    if (!rep || state.indexed || state.indexing) return;
    let man = state.serverManifests[String(rep.generator_id)];
    if (man === undefined) {
      man = rep.lake === false ? false : ((await fetchManifest(rep.dashboard_id)) || false);
      state.serverManifests[String(rep.generator_id)] = man;
      renderReports();
    }
    if (!man || state.indexed) return;
    loadServerIndex(rep);
  }

  // ── index ────────────────────────────────────────────────────────
  function isWorkbook(nm) {
    if (nm.startsWith('~$') || nm.startsWith('.')) return false;
    const lower = nm.toLowerCase();
    return lower.endsWith('.xlsx') || lower.endsWith('.xlsm')
        || lower.endsWith('.xls') || lower.endsWith('.xlsb');
  }

  // Returns {name, size, mtime_ms, read()} — `read()` is deferred on purpose:
  // the listing is cheap metadata and only the files the catalog actually
  // claims are ever pulled into memory as bytes.
  async function scanFolder(handle) {
    const tf = testFiles();
    if (tf) {
      return tf.filter(f => isWorkbook(f.name)).map(f => ({
        name: f.name, size: f.buf.byteLength,
        mtime_ms: f.mtime_ms || Date.now(),
        read: () => Promise.resolve(f.buf.slice(0)),
      }));
    }
    const out = [];
    for await (const entry of handle.values()) {
      if (entry.kind !== 'file' || !isWorkbook(entry.name)) continue;
      const f = await entry.getFile();
      out.push({
        name: entry.name, size: f.size, mtime_ms: f.lastModified,
        read: () => entry.getFile().then(x => x.arrayBuffer()),
      });
    }
    return out;
  }


  // ═════════════════════════════════════════════════════════════════
  // A folder with no report: tables "as is"
  // ═════════════════════════════════════════════════════════════════
  // "Explore" used to handle only folders declared in some report's
  // catalogue: `matchFilesToCatalog` matches file NAMES against table partitions,
  // and an ordinary folder with an export hit "no file matched" (owner,
  // 13.09 — he followed the course instructions and got stuck there). Yet both
  // engines accept a table name directly (`f.table` in `tableName`/`index_file`),
  // so the catalogue is needed for MATCHING, not for working. Hence: if
  // nothing matched, the page names the tables itself — from the file name — and
  // indexes the folder as is. Such tables have no metrics or semantics (nobody
  // declares them), but the SQL editor and the query builder work.
  function adhocTableName(file, taken) {
    let base = String(file || '').replace(/\.[^.]+$/, '')
      .replace(/[^0-9A-Za-z_\u0400-\u04FF]+/g, '_')
      .replace(/^_+|_+$/g, '').slice(0, 60) || 'file';
    if (/^\d/.test(base)) base = 't_' + base;
    if (/^_ep/i.test(base)) base = 't' + base;          // the engine's prefix is reserved
    let name = base, i = 2;
    while (taken.has(name)) name = base.slice(0, 58) + '_' + (i++);
    taken.add(name);
    return name;
  }

  function makeAdhocTables(files) {
    const taken = new Set((DATA.tables || []).map(tv => String(tv.name || '')));
    return (files || []).map(f => {
      const name = adhocTableName(f.name, taken);
      return {
        table_id: 'adhoc:' + name, name: name, display_name: f.name,
        status: 'confirmed', header_row: 0, adhoc: true, columns: [],
        partitions: [{ file: f.name, key: '', sheet: null }],
      };
    });
  }

  /* The columns of such a table are known only after parsing: ask the
     engine with one empty query, otherwise the builder has no column list. */
  async function fillAdhocColumns(tables) {
    for (const tv of tables) {
      try {
        const res = await send('explore:sql',
          { sql: 'SELECT * FROM "' + tv.name + '" LIMIT 0', limit: 1, final: false }, 15000);
        tv.columns = (res && res.columns ? res.columns : []).map(c => ({ name: c, type: 'text' }));
      } catch (e) { /* without a column list the builder is simply empty */ }
    }
  }

  async function runIndex() {
    if (!state.dirHandle || state.indexing) return;
    // A folder index is LOCAL by definition — leave server execution mode
    // before the worker builds anything.
    state.exec = 'local';
    state.execDash = null;
    state.indexing = true;
    try { await runIndexInner(); } finally { state.indexing = false; indexBtn.disabled = !state.dirHandle; }
  }
  async function runIndexInner() {
    indexBtn.disabled = true;
    progressWrap.hidden = false; openSources();
    setProgress(0, 1, t('Reading the folder…'));
    let files;
    try { files = await scanFolder(state.dirHandle); }
    catch (e) { setSummary(t('Failed to read the folder.')); indexBtn.disabled = false; return; }
    state.lastFiles = files;
    let { matched, unmatched } = matchFilesToCatalog(files, DATA.tables);
    state.unmatched = unmatched;
    state.matchedTableIds = Array.from(new Set(matched.map(m => m.table_id)));
    state.maxMtime = files.reduce((m, f) => Math.max(m, f.mtime_ms || 0), 0);
    if (unmatched.length) {
      setSummary(tn(unmatched.length,
        '{n} file not in catalogue: {list}|{n} files not in catalogue: {list}',
        {list: unmatched.slice(0, 5).join(', ') + (unmatched.length > 5 ? '…' : '')}));
    }
    let adhoc = [];
    if (!matched.length) {
      if (!files.length) {
        progressWrap.hidden = true; indexBtn.disabled = false;
        setSummary(t('The folder holds no workbook or table.'));
        return;
      }
      // A folder outside the catalogue — work with it as is.
      adhoc = makeAdhocTables(files);
      DATA.tables = (DATA.tables || []).filter(tv => !tv.adhoc).concat(adhoc);
      matched = adhoc.map((tv, i) => ({
        dir: 0, name: files[i].name, size: files[i].size, mtime_ms: files[i].mtime_ms,
        table: tv.name, table_id: tv.table_id, sheet: null, partition: '', header_row: 0,
      }));
      state.unmatched = [];
      state.matchedTableIds = adhoc.map(tv => tv.table_id);
      state.adhoc = true;
      setSummary(tn(adhoc.length,
        'The folder is not in the catalogue — taking it as it is: {n} file becomes table {list}.|The folder is not in the catalogue — taking it as it is: {n} files become tables {list}.',
        {list: adhoc.map(tv => tv.name).join(', ')}));
    } else {
      state.adhoc = false;
      DATA.tables = (DATA.tables || []).filter(tv => !tv.adhoc);
    }
    // Boot first: the worker rejects index before 'explore:ready'.
    setProgress(0, matched.length, t('Loading engine…'));
    const ok = await workerReady();
    if (!ok) { indexBtn.disabled = false; return; }
    // Ask the worker which partitions are already parsed in its Parquet
    // cache: those files are never read from the share at all.
    setProgress(0, matched.length, t('Checking the cache…'));
    const probe = await send('explore:cache_probe', {
      files: matched.map(r => ({ name: r.name, size: r.size, mtime_ms: r.mtime_ms, sheet: r.sheet, header_row: r.header_row })),
    }, 0);
    const cacheKeys = (probe && probe.keys) || [];
    const cacheHit = (probe && probe.cached) || [];
    // Phase 9.2: the keys of THIS index are what an export is made of, and
    // the parser version is what says whether somebody else's export can be
    // imported at all. Kept on the state so the (9.4) UI — and the Chrome
    // harness — never re-derive a key the worker owns the recipe for.
    state.cacheKeys = cacheKeys.slice();
    state.parserVersion = (probe && probe.parser_version) || null;
    // One file per message, awaited before the next is even read: the worker
    // writes each buffer straight to the Pyodide FS and drops it (O(1) on its
    // side), but batching every match into one `payload` array on THIS side
    // before the postMessage held the whole folder in the tab's memory at
    // once — for a real catalogue folder that is the sum of every workbook,
    // not the size of one. Reading one file at a time keeps the page's own
    // peak at a single workbook, matching the worker's discipline instead of
    // defeating it.
    const byName = new Map();
    files.forEach(f => byName.set(f.name, f));
    let indexed = 0;
    let cachedTotal = 0;
    const errors = [];
    let lastMsg = null;
    // Where the minutes go: the share read is the page's, the rest is the
    // worker's per-stage sum. Shown in the summary so nobody has to guess.
    const spent = { read: 0, fs_write: 0, parse: 0, coerce: 0, register: 0, view: 0, cache_write: 0, cache_read: 0, semantics: 0 };
    const addSpent = (res) => { const tv = (res && res.timings) || {}; Object.keys(tv).forEach(k => { if (k in spent) spent[k] += tv[k] || 0; }); };
    const tRun = Date.now();
    for (let i = 0; i < matched.length; i++) {
      const rec = matched[i];
      // The page is the only one that knows "file i of N": the worker gets
      // ONE file per message and would report 0→1 of 1 for each of them.
      state.indexDriver = { i: i, n: matched.length, cached: !!(cacheHit[i] && cacheKeys[i]) };
      if (cacheHit[i] && cacheKeys[i]) {
        setProgress(i, matched.length, t('From cache {p1}', {p1: rec.name}));
        const resC = await send('explore:index',
          { files: [Object.assign({}, rec, { cached: true, cache_key: cacheKeys[i] })], final: i === matched.length - 1 }, 0);
        indexed += resC.indexed || 0;
        cachedTotal += resC.cached || 0;
        (resC.errors || []).forEach(e => errors.push(e));
        addSpent(resC);
        lastMsg = resC;
        continue;
      }
      setProgress(i, matched.length, t('Reading {p1}', {p1: rec.name}));
      const src = byName.get(rec.name);
      if (!src) continue;
      let buf;
      const tRead = Date.now();
      try { buf = await src.read(); }
      catch (e) { errors.push({ name: rec.name, error: t('failed to read the file') }); continue; }
      setProgress(i, matched.length, t('Indexing {p1}', {p1: rec.name}));
      spent.read += Date.now() - tRead;
      const res = await send('explore:index',
        { files: [Object.assign({}, rec, { buf: buf, cache_key: cacheKeys[i] || null })], final: i === matched.length - 1 }, 0, [buf]);
      indexed += res.indexed || 0;
      addSpent(res);
      (res.errors || []).forEach(e => errors.push(e));
      lastMsg = res;
    }
    state.indexDriver = null;
    setProgress(matched.length, matched.length, t('Done'));
    spent.total = Date.now() - tRun;
    // These rows came out of the folder this browser just read, whatever the
    // page held before (a server index it is now replacing, for instance).
    state.source = 'folder';
    state.serverRun = null;
    applyIndexed({ indexed: indexed, cached: cachedTotal, errors: errors,
      tables: lastMsg ? lastMsg.tables : [], stats: lastMsg ? lastMsg.stats : {},
      semantics: lastMsg ? lastMsg.semantics : null, cache: lastMsg ? lastMsg.cache : null,
      peakWasmBytes: lastMsg ? lastMsg.peakWasmBytes : 0, spent: spent });
    // Columns of "as is" tables are requested AFTER the result is shown, without
    // waiting for the answer: it is a convenience for the builder, not part of the
    // index. Waiting here once swallowed the whole indexing result — the engine
    // did not answer, and the user was left watching progress instead of ready tables.
    if (adhoc.length) fillAdhocColumns(adhoc).then(populateTableSelect, () => {});
  }

  // ── query builder ────────────────────────────────────────────────
  // The catalogue is company-wide; the folder the user just connected is
  // usually one report's worth of it. Listing all of it flat makes the three
  // tables that actually have data behind them indistinguishable from the
  // eighty that do not — a query over an unindexed table runs and returns
  // nothing, which reads as a broken engine. So: the connected folder's
  // tables first, everything else behind a labelled group. Before any
  // indexing (`matchedTableIds` empty) the list is flat, as it always was.
  function populateTableSelect() {
    bTable.textContent = '';
    bTable.appendChild(el('option', { value: '', text: t('— select —') }));
    const usable = (DATA.tables || []).filter(tv => tv.status === 'confirmed' || tv.status === 'inferred');
    const inFolder = new Set(state.matchedTableIds || []);
    const opt = tv => el('option', { value: tv.table_id, text: tv.display_name || tv.name });
    if (!inFolder.size) {
      usable.forEach(tv => bTable.appendChild(opt(tv)));
      return;
    }
    const mine = usable.filter(tv => inFolder.has(tv.table_id));
    const rest = usable.filter(tv => !inFolder.has(tv.table_id));
    mine.forEach(tv => bTable.appendChild(opt(tv)));
    if (!rest.length) return;
    const group = el('optgroup', { label: t('Other catalogue tables') });
    rest.forEach(tv => group.appendChild(opt(tv)));
    bTable.appendChild(group);
  }

  function currentTable() { return tableByName(bTable.value); }

  function renderColumnPickers() {
    const tv = currentTable();
    bCols.textContent = ''; bGroupBy.textContent = ''; bAggCol.textContent = ''; bOrderCol.textContent = '';
    bAggCol.appendChild(el('option', { value: '', text: '*' }));
    bOrderCol.appendChild(el('option', { value: '', text: '—' }));
    if (!tv) return;
    (tv.columns || []).forEach(c => {
      const chip = el('span', { class: 'ex-b-col-chip', text: c.display || c.name, 'data-col': c.name, tabindex: '0', role: 'button' });
      chip.addEventListener('click', () => { chip.classList.toggle('active'); syncSql(); });
      bCols.appendChild(chip);
      bGroupBy.appendChild(el('option', { value: c.name, text: c.display || c.name }));
      bAggCol.appendChild(el('option', { value: c.name, text: c.display || c.name }));
      bOrderCol.appendChild(el('option', { value: c.name, text: c.display || c.name }));
    });
    renderFilterRows();
  }

  function selectedCols() {
    return Array.from(bCols.querySelectorAll('.ex-b-col-chip.active')).map(c => c.dataset.col);
  }
  function selectedGroupBy() {
    return Array.from(bGroupBy.selectedOptions || []).map(o => o.value).filter(Boolean);
  }

  function renderFilterRows() {
    bFilters.textContent = '';
    const tv = currentTable();
    state.filterRows.forEach((row, idx) => {
      const colSel = el('select', { class: 'input sm ex-b-filter-col' });
      colSel.appendChild(el('option', { value: '', text: t('— column —') }));
      (tv ? tv.columns || [] : []).forEach(c => colSel.appendChild(el('option', { value: c.name, text: c.display || c.name, selected: c.name === row.column })));
      colSel.addEventListener('change', () => { row.column = colSel.value; syncSql(); });

      const opSel = el('select', { class: 'input sm' });
      ['=', '!=', '>', '>=', '<', '<=', 'LIKE', 'IN', 'IS NULL', 'IS NOT NULL'].forEach(op => {
        opSel.appendChild(el('option', { value: op, text: op, selected: op === row.op }));
      });
      opSel.addEventListener('change', () => { row.op = opSel.value; syncSql(); });

      const valIn = el('input', { class: 'input sm ex-b-filter-val', value: row.value || '', list: 'ex-vals-' + idx });
      valIn.addEventListener('input', () => { row.value = valIn.value; syncSql(); });
      const dl = el('datalist', { id: 'ex-vals-' + idx });
      const known = tv ? (DATA.values && DATA.values[tv.table_id + '.' + row.column]) : null;
      (known || []).forEach(v => dl.appendChild(el('option', { value: v })));

      const rmBtn = el('button', { class: 'btn xs ghost', type: 'button', text: '×' });
      rmBtn.addEventListener('click', () => { state.filterRows.splice(idx, 1); renderFilterRows(); syncSql(); });

      bFilters.appendChild(el('div', { class: 'ex-b-filter-row' }, [colSel, opSel, valIn, dl, rmBtn]));
    });
  }

  function columnTypesOf(tv) {
    const map = {};
    (tv ? tv.columns || [] : []).forEach(c => { map[c.name] = c.type; });
    return map;
  }

  function currentSpec() {
    const tv = currentTable();
    return {
      table: tv ? (tv.name || tv.table_id) : '',
      columns: selectedCols(),
      filters: state.filterRows.filter(f => f.column && f.op),
      columnTypes: columnTypesOf(tv),
      groupBy: selectedGroupBy(),
      aggFn: bAggFn.value,
      aggCol: bAggCol.value,
      orderCol: bOrderCol.value,
      orderDir: bOrderDir.value,
      limit: bLimit.value,
    };
  }

  function syncSql() {
    if (!currentTable()) return;
    sqlBox.value = buildSql(currentSpec());
  }

  bTable && bTable.addEventListener('change', () => { renderColumnPickers(); syncSql(); });
  bFilterAdd && bFilterAdd.addEventListener('click', () => { state.filterRows.push({ column: '', op: '=', value: '' }); renderFilterRows(); });
  [bGroupBy, bAggFn, bAggCol, bOrderCol, bOrderDir, bLimit].forEach(elx => elx && elx.addEventListener('change', syncSql));
  bLimit && bLimit.addEventListener('input', syncSql);

  // ── recipes ──────────────────────────────────────────────────────
  const RECIPES = window.OP_EXPLORE_RECIPES || null;

  function tableOptions(select) {
    select.textContent = '';
    (DATA.tables || []).forEach(tv => select.appendChild(el('option', { value: tv.table_id, text: tv.display_name || tv.name })));
  }
  function colOptions(select, tableId) {
    select.textContent = '';
    const tv = tableById(tableId);
    (tv ? tv.columns || [] : []).forEach(c => select.appendChild(el('option', { value: c.name, text: c.display || c.name })));
  }
  function partOptions(select, tableId) {
    select.textContent = '';
    select.appendChild(el('option', { value: '', text: t('(all partitions)') }));
    const tv = tableById(tableId);
    (tv ? tv.partitions || [] : []).forEach(p => select.appendChild(el('option', { value: p.key, text: p.key })));
  }
  // Recipes that scope a query to one partition (dupes/gaps, reconcile) default
  // to the most recently loaded one — the "current state" reading a user opens
  // the recipe expecting — rather than leaving the WHERE clause unscoped.
  function selectLastPartition(select) {
    const opts = select.options || [];
    if (opts.length) select.value = opts[opts.length - 1].value;
  }

  function recipeForm_changed() {
    const tSel = el('select', { class: 'input sm' }); tableOptions(tSel);
    const keySel = el('select', { class: 'input sm' });
    const pA = el('select', { class: 'input sm' });
    const pB = el('select', { class: 'input sm' });
    function refresh() { colOptions(keySel, tSel.value); partOptions(pA, tSel.value); partOptions(pB, tSel.value); }
    tSel.addEventListener('change', refresh);
    refresh();
    const go = el('button', { class: 'btn xs', type: 'button', text: t('Generate SQL') });
    go.addEventListener('click', () => {
      const tv = tableById(tSel.value);
      if (!tv) return;
      const cols = (tv.columns || []).map(c => c.name);
      sqlBox.value = RECIPES.whatChanged(tv.name || tv.table_id, keySel.value, pA.value, pB.value, cols);
    });
    return [tSel, keySel, pA, pB, go];
  }
  function recipeForm_asof() {
    const tSel = el('select', { class: 'input sm' }); tableOptions(tSel);
    const pSel = el('select', { class: 'input sm' });
    function refresh() { partOptions(pSel, tSel.value); }
    tSel.addEventListener('change', refresh); refresh();
    const go = el('button', { class: 'btn xs', type: 'button', text: t('Generate SQL') });
    go.addEventListener('click', () => {
      const tv = tableById(tSel.value);
      if (!tv) return;
      sqlBox.value = RECIPES.asOfDate(tv.name || tv.table_id, pSel.value);
    });
    return [tSel, pSel, go];
  }
  function recipeForm_reconcile() {
    const tA = el('select', { class: 'input sm' }); tableOptions(tA);
    const tB = el('select', { class: 'input sm' }); tableOptions(tB);
    const kA = el('select', { class: 'input sm' });
    const kB = el('select', { class: 'input sm' });
    const pA = el('select', { class: 'input sm' });
    const pB = el('select', { class: 'input sm' });
    // Reconciliation compares full history by default (no partition scoping):
    // narrowing to "the latest partition only" would miss an order realised
    // in an earlier or later partition than the one it was placed in — the
    // whole point of a cross-table reconciliation. A user narrows explicitly
    // via the dropdown when they want one partition's snapshot instead.
    function refreshA() { colOptions(kA, tA.value); partOptions(pA, tA.value); }
    function refreshB() { colOptions(kB, tB.value); partOptions(pB, tB.value); }
    tA.addEventListener('change', refreshA); tB.addEventListener('change', refreshB);
    refreshA(); refreshB();
    const go = el('button', { class: 'btn xs', type: 'button', text: t('Generate SQL') });
    go.addEventListener('click', () => {
      const a = tableById(tA.value), b = tableById(tB.value);
      if (!a || !b) return;
      sqlBox.value = RECIPES.reconcile(a.name || a.table_id, b.name || b.table_id, kA.value, kB.value, pA.value, pB.value);
    });
    return [tA, kA, pA, tB, kB, pB, go];
  }
  function recipeForm_dupgaps() {
    const tSel = el('select', { class: 'input sm' }); tableOptions(tSel);
    const kSel = el('select', { class: 'input sm' });
    const pSel = el('select', { class: 'input sm' });
    function refresh() { colOptions(kSel, tSel.value); partOptions(pSel, tSel.value); selectLastPartition(pSel); }
    tSel.addEventListener('change', refresh); refresh();
    const go = el('button', { class: 'btn xs', type: 'button', text: t('Generate SQL') });
    go.addEventListener('click', () => {
      const tv = tableById(tSel.value);
      if (!tv) return;
      sqlBox.value = RECIPES.dupsAndGaps(tv.name || tv.table_id, kSel.value, pSel.value);
    });
    return [tSel, kSel, pSel, go];
  }
  function recipeForm_trend() {
    const tSel = el('select', { class: 'input sm' }); tableOptions(tSel);
    const mSel = el('select', { class: 'input sm' });
    function refresh() {
      mSel.textContent = '';
      mSel.appendChild(el('option', { value: '', text: 'COUNT(*)' }));
      const tv = tableById(tSel.value);
      (tv ? tv.columns || [] : []).forEach(c => mSel.appendChild(el('option', { value: c.name, text: 'SUM(' + (c.display || c.name) + ')' })));
    }
    tSel.addEventListener('change', refresh); refresh();
    const go = el('button', { class: 'btn xs', type: 'button', text: t('Generate SQL') });
    go.addEventListener('click', () => {
      const tv = tableById(tSel.value);
      if (!tv) return;
      sqlBox.value = RECIPES.trendByPartition(tv.name || tv.table_id, mSel.value || null);
    });
    return [tSel, mSel, go];
  }

  const RECIPE_FORMS = {
    changed: recipeForm_changed, asof: recipeForm_asof, reconcile: recipeForm_reconcile,
    dupgaps: recipeForm_dupgaps, trend: recipeForm_trend,
  };

  if (recipeList) {
    Array.from(recipeList.querySelectorAll('[data-recipe]')).forEach(btn => {
      btn.addEventListener('click', () => {
        Array.from(recipeList.querySelectorAll('.ex-recipe-btn')).forEach(b => b.classList.toggle('active', b === btn));
        recipeForm.textContent = '';
        const build = RECIPE_FORMS[btn.dataset.recipe];
        if (build) build().forEach(elx => recipeForm.appendChild(elx));
      });
    });
  }

  // Which partition keys the SQL text explicitly restricts `__partition` to
  // (via `= 'x'` or `IN ('x','y')`), if any. This is a badge-accuracy aid,
  // not a security check: `_context()` on the server (static/py/_explore.py)
  // reports every loaded partition of every table the statement NAMES, with
  // no idea what the WHERE clause actually selected — so a one-partition
  // "As of date" query over a 12-partition table comes back claiming all
  // 12 were read. Scanning the same SQL for a literal `__partition` filter
  // lets the badge narrow "used" down to the partition(s) the query could
  // possibly have touched, without needing a real SQL parser.
  function partitionLiteralsInSql(sql) {
    const vals = new Set();
    const unescape = s => s.replace(/''/g, "'");
    const reEq = /__partition\s*=\s*'((?:[^']|'')*)'/gi;
    let m;
    while ((m = reEq.exec(sql))) vals.add(unescape(m[1]));
    const reIn = /__partition\s+IN\s*\(([^)]*)\)/gi;
    while ((m = reIn.exec(sql))) {
      const reLit = /'((?:[^']|'')*)'/g;
      let mm;
      while ((mm = reLit.exec(m[1]))) vals.add(unescape(mm[1]));
    }
    return vals;
  }

  // Newest mtime among the catalog partitions this answer actually read.
  // Falls back to the folder scan when the badge names partitions the
  // snapshot doesn't carry (an unknown key can only make the date older,
  // never newer, so the fallback never overstates freshness).
  function freshnessOf(used) {
    let best = 0;
    (used || []).forEach(u => {
      (DATA.tables || []).forEach(tv => {
        if (tv.name !== u.table) return;
        (tv.partitions || []).forEach(p => {
          if (p.key === u.key && p.mtime_ms) best = Math.max(best, p.mtime_ms);
        });
      });
    });
    return best || state.maxMtime || 0;
  }

  // ── run / result grid ────────────────────────────────────────────
  function renderResult(res) {
    state.lastResult = res;
    resultThead.textContent = '';
    resultTbody.textContent = '';
    const cols = res.columns || [];
    cols.forEach((c, i) => {
      const th = el('th', { text: c });
      if (state.sortCol === i) th.classList.add(state.sortDir > 0 ? 'sorted-asc' : 'sorted-desc');
      th.addEventListener('click', () => {
        state.sortDir = (state.sortCol === i) ? -state.sortDir : 1;
        state.sortCol = i;
        renderResult(res);
      });
      resultThead.appendChild(th);
    });
    let rows = (res.rows || []).slice();
    if (state.sortCol != null && cols.length) {
      const i = state.sortCol, dir = state.sortDir;
      rows.sort((a, b) => {
        const av = a[i], bv = b[i];
        if (av == null && bv == null) return 0;
        if (av == null) return 1; if (bv == null) return -1;
        return av > bv ? dir : av < bv ? -dir : 0;
      });
    }
    const vcol = cols.indexOf('value');
    const kpiFmt = state.lastKpi;
    rows.forEach(r => {
      const tr = el('tr', {});
      r.forEach((v, ci) => tr.appendChild(el('td', {
        text: v == null ? '' : (ci === vcol && kpiFmt ? fmtValue(v, kpiFmt) : String(v)) })));
      resultTbody.appendChild(tr);
    });
    resultEmpty.hidden = rows.length !== 0;

    resultBadge.hidden = false;
    const allUsed = res.partitions_used || [];
    // Narrow to the partition(s) the SQL's own WHERE clause named, when it
    // named any — see partitionLiteralsInSql(). "expected" always stays the
    // server's full count so the badge still answers "of how many total".
    const literals = partitionLiteralsInSql(state.lastSql || '');
    const used = literals.size ? allUsed.filter(u => literals.has(u.key)) : allUsed;
    // The same narrowed set "how it's calculated" must list — computed once here,
    // read there, so the badge and the sources can never disagree.
    state.lastUsed = used;
    const expected = res.partitions_expected != null ? res.partitions_expected : allUsed.length;
    let text = t('{p1} of {p2} {ctx:2}', {p1: used.length, p2: nOf(expected, [t('partitions'), t('partitions {ctx:2}'), t('partitions {ctx:2}')])});
    const asOf = freshnessOf(used);
    if (asOf) text += t(' · data as of {p1}', {p1: fmtDate(asOf)});
    text += ' · ' + (res.total != null && res.total > rows.length
      ? t('{shown} of {total}', {shown: rows.length,
                                total: tn(res.total, '{n} row|{n} rows {ctx:2}')})
      : tn(rows.length, '{n} row|{n} rows'));
    if (res.incomplete) {
      const miss = (res.missing_partitions || []).length;
      text += t(' · incomplete{p1}', {p1: (miss ? t(' (no {p1} partitions)', {p1: miss}) : res.truncated ? t(' (truncated by limit)') : '')});
    }
    resultBadge.textContent = text;
    resultBadge.classList.toggle('is-incomplete', !!res.incomplete);
    resultMs.textContent = res.ms != null ? t('{p1} ms', {p1: res.ms}) : '';
  }

  function setSqlStatus(text) { if (sqlStatus) sqlStatus.textContent = text || ''; }

  // ── the answer card: title, "how it's calculated", the SQL row ─────────
  // The status line is a sentence assembled with « · » separators; its FIRST
  // clause is the answer itself (metric, cut, period) and the rest is
  // provenance/notes. The card's title is that first clause.
  function firstClause(text) { return String(text || '').split(' · ')[0]; }
  function setAnswerTitle(text) { if (answerTitle) answerTitle.textContent = text || ''; }

  // What the NEXT runQuery() is an answer to. Set by the intent/metric paths
  // right before they run; a bare SQL/builder run leaves it null and the card
  // falls back to the typed question.
  function primeAnswer(title, kpi, intent) {
    state.pendingAnswer = { title: title || '', kpi: kpi || null, intent: intent || null };
  }

  function setSqlRowOpen(open) { if (sqlRow) sqlRow.open = !!open; }

  // 1 234 567 with thin spaces — a ratio's numerator and denominator are read
  // side by side, and unseparated digits are what make them unreadable.
  // A value in the metric's own terms. `measure: ratio` is a share → percent
  // with two decimals; anything with a `unit` carries it («45,84 mln st»);
  // the rest is a plain number. Only the `value` column is formatted this
  // way — n / d and dims are shown as they are.
  function fmtValue(v, kpi) {
    if (v == null || v === '') return '';
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    const k = kpi || {};
    if (k.measure === 'ratio') {
      // n/d is not always a share: duration = stock/sales is DAYS. A declared
      // unit wins over the percent guess, and a "share" of 274% is not a
      // share — show the raw number rather than lie in percent (prod
      // 2026-08-24, Van Stock duration).
      const ru = k.unit ? String(k.unit) : '';
      // A ratio the author declared in percent IS a share: 0.9998 → 99,98 %
      // (prod 2026-08-28 showed «0.9998 %»). A value already above 1.5 is
      // taken as percent points as written.
      if (ru === '%' || ru === 'percent' || ru === 'проц' || ru === 'процент') {
        return (Math.abs(n) > 1.5 ? n : n * 100).toFixed(2).replace('.', ',') + ' %';
      }
      if (ru) return fmtNum(n) + ' ' + ru;
      if (Math.abs(n) > 1.5) return fmtNum(n);
      return (n * 100).toFixed(2).replace('.', ',') + ' %';
    }
    const unit = k.unit ? String(k.unit) : '';
    if (unit === 'pieces' || unit === 'шт' || unit === 'pcs') return t('{p1} items', {p1: fmtNum(Math.round(n))});
    return fmtNum(n) + (unit ? ' ' + unit : '');
  }
  function fmtNum(v) {
    if (v == null || v === '') return '';
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    const s = String(Math.abs(n) < 1000 ? Math.round(n * 10000) / 10000 : Math.round(n * 100) / 100);
    const parts = s.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    return parts.join('.');
  }

  // «n / d = value» for a ratio KPI (the shape `intent_sql` emits), otherwise
  // the declared aggregation over `value`. With no KPI behind the result at
  // all (a hand-written SQL run) there is nothing honest to say — the panel
  // then shows only its sources.
  function howCalcText(res, kpi) {
    const cols = (res && res.columns) || [];
    const row = ((res && res.rows) || [])[0];
    const iN = cols.indexOf('n'), iD = cols.indexOf('d'), iV = cols.indexOf('value');
    if (row && iN >= 0 && iD >= 0 && iV >= 0) {
      return fmtNum(row[iN]) + ' / ' + fmtNum(row[iD]) + ' = ' + fmtNum(row[iV]);
    }
    if (!kpi) return '';
    const agg = String(kpi.aggregation || '').trim();
    return agg ? agg.toUpperCase() + '(value)' : 'value';
  }

  // Which partition files this answer stands on: the badge's own «used» set,
  // resolved back to the catalog's file name and the mtime the folder scan
  // read for it (the freshest truth available on the page).
  function sourceLine(u) {
    let file = '', when = 0;
    (DATA.tables || []).forEach(tv => {
      if (tv.name !== u.table) return;
      (tv.partitions || []).forEach(p => {
        if (p.key !== u.key) return;
        file = p.file || file;
        when = Math.max(when, p.mtime_ms || 0);
      });
    });
    const f = (state.lastFiles || []).find(x => x.name === file);
    if (f && f.mtime_ms) when = f.mtime_ms;
    return [u.key, file, when ? fmtDate(when) : ''].filter(Boolean).join(' · ');
  }

  // The provenance of the answer currently on screen, as data: which
  // partitions it stands on, the freshest mtime among them, and the files
  // behind them. `explore-chat.js` freezes this into every thread record, so
  // a bubble reopened next month still says what its number stood on — the
  // catalog snapshot it was derived from may be long gone by then.
  function answerProvenance() {
    const used = state.lastUsed || [];
    const files = [];
    used.forEach(u => {
      (DATA.tables || []).forEach(tv => {
        if (tv.name !== u.table) return;
        (tv.partitions || []).forEach(p => { if (p.key === u.key && p.file) files.push(p.file); });
      });
    });
    return {
      partitions_used: used.map(u => ({ table: u.table, key: u.key })),
      as_of: freshnessOf(used) || null,
      files: Array.from(new Set(files)),
      // Phase 9.4: WHERE the rows behind this number came from. A bubble
      // reopened next month must not read as "my folder said so" when it was
      // really the server index of one particular run.
      source: state.source || null,
      run_id: state.source === 'server' ? state.serverRun : null,
    };
  }

  function renderHow(res) {
    if (!howBox) return;
    const kpi = state.lastKpi;
    let def = '';
    if (kpi) {
      def = kpi.definition || kpi.name || '';
      if (kpi.target != null && kpi.target !== '') def += t('{p1}target: {p2}', {p1: (def ? ' · ' : ''), p2: kpi.target});
    }
    if (howDef) { howDef.textContent = def; howDef.hidden = !def; }
    const calc = howCalcText(res, kpi);
    if (howCalc) { howCalc.textContent = calc; howCalc.hidden = !calc; }
    const used = state.lastUsed || [];
    if (howSources) {
      howSources.textContent = '';
      used.forEach(u => howSources.appendChild(el('div', { class: 'ex-how-src', text: sourceLine(u) })));
      howSources.hidden = !used.length;
    }
    howBox.hidden = !(def || calc || used.length);
  }

  // An index that is being built is not a reason to refuse — wait for it.
  // Resolves true when indexed, false when the build ended without one.
  function whenIndexed() {
    if (state.indexed) return Promise.resolve(true);
    if (!state.indexing) return Promise.resolve(false);
    return new Promise(res => {
      const tv = setInterval(() => {
        if (state.indexed) { clearInterval(tv); res(true); }
        else if (!state.indexing) { clearInterval(tv); res(false); }
      }, 300);
      if (tv && typeof tv.unref === 'function') tv.unref();
    });
  }
  function noIndexText() {
    if (state.indexing) return t('Index is being built — the query will run as soon as it is ready.');
    const rep = focusedReport();
    const man = rep && state.serverManifests[String(rep.generator_id)];
    if (man) return t('Index not loaded — click "Load index" on the report card.');
    return t('Load the report index first.');
  }
  async function runQuery() {
    const sql = sqlBox.value.trim();
    if (!sql) return;
    if (!state.indexed) {
      setSqlStatus(noIndexText());
      if (!(await whenIndexed())) return;
      setSqlStatus('');
    }
    runBtn.disabled = true;
    setSqlStatus(t('Running…'));
    // A visible clock while the engine works: nothing else moves on the
    // page during a long query, and a still page reads as "nothing ran".
    const t0 = Date.now(); let nlTick = null;   // nlTick: the last clock text written to nlStatus
    const tick = () => {
      const secs = Math.round((Date.now() - t0) / 1000);
      setSqlStatus(t('Calculating… {p1} s', {p1: secs}));
      if (answerTitle) answerTitle.textContent = t('{p1} — calculating… {p2} s', {p1: titleBase, p2: secs});
      if (nlStatus) nlStatus.textContent = nlTick = t('Calculating… {p1} s', {p1: secs});
    };
    const ticker = setInterval(tick, 1000);
    if (ticker && typeof ticker.unref === 'function') ticker.unref();
    if (abortBtn) abortBtn.hidden = false;
    // A new query invalidates any sort applied to the previous result's
    // column layout (a stale index can point past the new column count,
    // or silently sort by the wrong column) and it is the SQL this run's
    // badge/freshness must be read against.
    state.sortCol = null; state.sortDir = 1;
    state.lastSql = sql;
    // Whatever primed this run owns the answer card; a bare SQL/builder run
    // has no KPI behind it, so the "how it's calculated" panel must not keep
    // showing the previous answer's definition.
    const prime = state.pendingAnswer;
    state.pendingAnswer = null;
    state.lastKpi = prime ? prime.kpi : null;
    state.lastIntent = prime ? prime.intent : null;
    const title = (prime && prime.title)
      || ((nlBox && nlBox.value.trim()) || t('Query result'));
    state.lastAnswer = { title: title, sql: sql };
    const titleBase = title;
    setAnswerTitle(title);
    // Computed BEFORE the run (not after, where the tier-1 paths null it
    // out) so the one recordable `explore:sql` call below can carry the
    // same question the thread bubble ends up showing.
    const qForRecord = state.askQuestion || (nlBox && nlBox.value.trim()) || title;
    // "Cancel" on the chat's pending card, gate 1: the engine is never
    // asked to run a query nobody is waiting for.
    if (state.askCancelled) {
      clearInterval(ticker);
      if (abortBtn) abortBtn.hidden = true;
      runBtn.disabled = false;
      setSqlStatus(t('Cancelled.'));
      setAnswerTitle(titleBase);
      emit('cancelled', { question: qForRecord });
      state.askCancelled = false;
      state.askQuestion = null; state.askSource = null; state.askVia = null;
      state.askMatch = null; state.askShortlist = null; state.askAnswerId = null;
      state.askParentN = null; state.askRegen = false;
      return;
    }
    emit('stage', { stage: 'exec' });
    try {
      const res = await send('explore:sql',
        { sql: sql, limit: Number(bLimit.value) || 1000, final: true, question: qForRecord },
        HARD_QUERY_TIMEOUT_MS);
      clearInterval(ticker);
      if (res.error) {
        const msg = t('Query error: {p1}', {p1: res.error});
        setSqlStatus(msg);
        setAnswerTitle(t('{p1} — failed', {p1: titleBase}));
        if (nlStatus) nlStatus.textContent = msg;
        // The server's answer id belongs to the answer that FAILED. Left
        // standing, `answerIdOf` hands it to the NEXT answer (which the
        // server may not have recorded at all) and a 👍 lands on the wrong
        // row — same reset the emit path does.
        state.askAnswerId = null;
        return;
      }
      setSqlStatus('');
      setAnswerTitle(titleBase);
      if (nlStatus && nlTick !== null && nlStatus.textContent === nlTick) nlStatus.textContent = '';
      renderResult(res);
      renderHow(res);
      // ONE 'answer' event per completed answer. Every tier-1 path
      // (finishIntent, runMetric, answerFormMatch) ends here, so emitting
      // from this single place is what keeps the thread from recording the
      // same answer twice; `whyCard` never reaches runQuery and emits its own.
      const q = state.askQuestion || (nlBox && nlBox.value.trim()) || title;
      const src = state.askSource || state.askVia || 'ask';
      const match = state.askMatch, shortlist = state.askShortlist, regen = state.askRegen;
      state.askQuestion = null; state.askSource = null; state.askVia = null;
      state.askMatch = null; state.askShortlist = null; state.askRegen = false;
      const kind = state.lastIntent ? 'kpi' : 'sql';
      state.openAnswer = { question: q, intent: state.lastIntent, kpi: state.lastKpi,
        sql: sql, kind: kind };
      // Gate 2: cancelled while the engine was running. The number is on
      // screen A (it was computed), but the thread gets no record — the chat
      // writes its own "cancelled" line instead of an orphan card.
      if (state.askCancelled) {
        state.askCancelled = false;
        state.lastNotes = []; state.askParentN = null; state.askAnswerId = null;
        emit('cancelled', { question: q });
        return;
      }
      emit('answer', { question: q, intent: state.lastIntent, kpi: state.lastKpi,
        sql: sql, res: res, kind: kind, source: src, parent_n: state.askParentN || null,
        match: match, shortlist: shortlist, regen: regen, answer_id: answerIdOf(res),
        notes: (state.lastNotes || []).slice() });
      state.askAnswerId = null;
      state.lastNotes = [];
      state.askParentN = null;
    } catch (e) {
      clearInterval(ticker);
      setSqlStatus(t('Failed to run the query.'));
      setAnswerTitle(t('{p1} — failed', {p1: titleBase}));
      // Same reset as the error branch above: a failed answer's id must not
      // be inherited by the next one (see `answerIdOf`).
      state.askAnswerId = null;
      reportClientError('query_failed', (e && e.message) || String(e),
        { sql: String(sql || '').slice(0, 400), tier: state.lastIntent ? 1 : 2 });
    } finally {
      clearInterval(ticker);
      if (abortBtn) abortBtn.hidden = true;
      runBtn.disabled = false;
      indexBtn.disabled = !state.dirHandle;
    }
  }

  runBtn && runBtn.addEventListener('click', runQuery);
  // Ctrl/⌘+Enter runs from anywhere on the page except while typing into the
  // natural-language question box, where Enter belongs to the textarea.
  document.addEventListener('keydown', e => {
    if (!(e.metaKey || e.ctrlKey) || e.key !== 'Enter') return;
    if (e.target === nlBox) return;
    e.preventDefault();
    runQuery();
  });

  // ── saved questions ──────────────────────────────────────────────
  // Which catalog tables a SQL string names — used for the `table_ids` both
  // /questions and /nl2sql want. Crude on purpose (a substring scan over the
  // known names): it labels a saved row and scopes an LLM prompt, it never
  // decides access — the server re-checks reach on every call.
  function tablesInSql(sql) {
    const low = String(sql || '').toLowerCase();
    const ids = (DATA.tables || []).filter(tv => tv.name && low.indexOf(String(tv.name).toLowerCase()) >= 0)
      .map(tv => tv.table_id);
    if (ids.length) return ids;
    const cur = currentTable();
    return cur ? [cur.table_id] : [];
  }

  function renderSaved(list) {
    if (!savedList) return;
    savedList.textContent = '';
    (list || []).forEach(q => {
      const btn = el('button', { class: 'ex-saved-btn', type: 'button', text: q.title, title: q.title });
      btn.addEventListener('click', () => {
        sqlBox.value = q.sql || '';
        if (nlBox && q.question) nlBox.value = q.question;
        setSqlStatus(t('Loaded question "{p1}"', {p1: q.title}));
      });
      const row = el('div', { class: 'ex-saved-row', 'data-question-id': String(q.id) }, [btn]);
      if (q.mine) {
        const del = el('button', { class: 'btn xs ghost', type: 'button', text: '×', title: t('Delete') });
        del.addEventListener('click', async () => {
          try { await fetch('/api/explore/questions/' + q.id, { method: 'DELETE' }); } catch (e) { /* best-effort */ }
          loadSaved();
        });
        row.appendChild(del);
      }
      savedList.appendChild(row);
    });
    if (savedEmpty) savedEmpty.hidden = (list || []).length > 0;
  }

  async function loadSaved() {
    try {
      const r = await fetch('/api/explore/questions', { headers: { 'Accept': 'application/json' } });
      if (!r.ok) return;
      const data = await r.json();
      renderSaved(data.questions || []);
    } catch (e) { /* best-effort */ }
  }

  saveBtn && saveBtn.addEventListener('click', async () => {
    const sql = sqlBox.value.trim();
    if (!sql) { setSqlStatus(t('Nothing to save — SQL is empty.')); return; }
    const title = ((saveTitle && saveTitle.value) || '').trim()
      || (nlBox && nlBox.value.trim().slice(0, 120))
      || t('Question from {p1}', {p1: OP_I18N.fmtDate(new Date())});
    saveBtn.disabled = true;
    try {
      const r = await fetch('/api/explore/questions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title, question: (nlBox && nlBox.value) || null,
          sql: sql, table_ids: tablesInSql(sql),
        }),
      });
      if (!r.ok) { setSqlStatus(t('Failed to save ({p1}).', {p1: r.status})); return; }
      if (saveTitle) saveTitle.value = '';
      setSqlStatus(t('Question "{p1}" saved.', {p1: title}));
      loadSaved();
    } catch (e) {
      setSqlStatus(t('Failed to save the question.'));
    } finally {
      saveBtn.disabled = false;
    }
  });

  // ── export ───────────────────────────────────────────────────────
  exportBtn && exportBtn.addEventListener('click', async () => {
    const sql = sqlBox.value.trim();
    if (!sql) return;
    if (!state.indexed) {
      setSqlStatus(noIndexText());
      if (!(await whenIndexed())) return;
    }
    exportBtn.disabled = true;
    setSqlStatus(t('Preparing file…'));
    try {
      const res = await send('explore:export', { sql: sql }, HARD_QUERY_TIMEOUT_MS);
      if (res && res.error) { setSqlStatus(t('Export error: {p1}', {p1: res.error})); exportBtn.disabled = false; return; }
      if (res && res.buf && res.name) {
        const blob = new Blob([res.buf], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
        const url = URL.createObjectURL(blob);
        const a = el('a', { href: url, download: res.name });
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        setSqlStatus(t('File {p1} exported.', {p1: res.name}));
      }
    } catch (e) { setSqlStatus(t('Failed to export the file.')); }
    exportBtn.disabled = false;
  });

  // ── NL → SQL (never auto-runs) ──────────────────────────────────
  // The server caps table_ids at 32 and rejects an empty list, but the real
  // ceiling is the operator's CPU: every table costs prompt tokens, and a
  // full 20-table catalog is ~8k tokens — minutes of prompt-eval on a
  // CPU-only Ollama. Scope down in layers, widest useful set first:
  // tables the question names, then the connected folder's tables (keeps
  // cross-table questions over the data actually being explored), then the
  // builder's pick, and only then a capped slice of everything.
  function nlTableIds() {
    const all = DATA.tables || [];
    const q = ((nlBox && nlBox.value) || '').toLowerCase();
    const named = all.filter(tv => [tv.name, tv.display_name].some(
      n => n && q.indexOf(String(n).toLowerCase()) >= 0));
    if (named.length) return named.map(tv => tv.table_id).slice(0, 32);
    const folder = state.matchedTableIds || [];
    if (folder.length) return folder.slice(0, 32);
    const cur = currentTable();
    if (cur) return [cur.table_id];
    // No signal at all: a wide prompt is minutes of prompt-eval on a
    // CPU-only Ollama, so send only a few tables and say how to do better.
    if (nlStatus) nlStatus.textContent =
      t('Asking… Tip: select a table in the builder or name it in the question — the answer will come noticeably faster.');
    return all.map(tv => tv.table_id).slice(0, 4);
  }

  // Intent route (ADR-13): the model picks a KPI + dims + filters from the
  // generator's own definitions; the worker turns that into SQL. Falls back
  // to bare nl2sql when nothing semantic is indexed. Every ask* function
  // below returns true when the question was answered, false when it was
  // "not a KPI question" (422) and the caller should fall through to bare
  // nl2sql; a reported failure also returns true — nothing else to try.
  //
  // ── which side calls the model (docs/LLM_PROVIDERS.md) ───────────────
  // Prod measurement: the server container has no egress to the provider,
  // the users' browsers do (through the corporate proxy). So the model call
  // can live in the page: the server still builds the prompt (prepare) and
  // still validates the answer (validate) — the page only carries the text
  // across. `llm-config` says whether that is switched on and with which
  // key; the key is either handed out by the server (OP_LLM_BROWSER_KEY, to
  // users who may run a generator) or typed by the user (BYOK) and kept in
  // localStorage — a BYOK key never touches OnePage's server.
  const LLM_KEY_STORE = 'op-llm-key';
  const keyRow = document.getElementById('ex-llm-key-row');
  const keyInput = document.getElementById('ex-llm-key');
  function storedKey() {
    try { return localStorage.getItem(LLM_KEY_STORE) || ''; } catch (e) { return ''; }
  }

  keyInput && keyInput.addEventListener('change', () => {
    const v = (keyInput.value || '').trim();
    try { v ? localStorage.setItem(LLM_KEY_STORE, v) : localStorage.removeItem(LLM_KEY_STORE); } catch (e) {}
    nlStatus.textContent = v ? t('Key saved in this browser.') : t('Key deleted.');
  });

  // Read per question rather than cached for the tab's lifetime: it is one
  // small GET in front of a call that takes seconds anyway, and it means a
  // rotated server key (or the flag being switched off) reaches an open tab
  // with the next question instead of with the next reload.
  async function llmConfig() {
    let cfg = {};
    try {
      const r = await fetch('/api/explore/llm-config');
      cfg = r.ok ? await r.json() : {};
    } catch (e) { cfg = {}; }
    // BYOK row: only when the page may call the model and the server holds
    // no key for this user. Nothing to type otherwise.
    if (keyRow) {
      keyRow.hidden = !(cfg.browser && !cfg.key);
      if (!keyRow.hidden && keyInput && !keyInput.value) keyInput.value = storedKey();
    }
    return cfg;
  }

  function browserKey(cfg) { return (cfg && cfg.key) || storedKey(); }

  // Host name for the status line — "couldn't reach api.deepseek.com" is
  // the message an operator can act on; a bare "network error" is not.
  // Client-side failure log (2026-08-28): everything after the server's
  // part — the provider call, the engine, this page's JS — fails in the
  // browser and used to leave no trace anywhere an admin could look. One
  // fire-and-forget row per failure into `audit_log` (`explore_client_error`);
  // never a cell value — the question, the stage, the text the page showed,
  // the status, the host, and for tier 2 the model's own SQL the engine refused.
  function reportClientError(stage, reason, extra) {
    try {
      const body = Object.assign({
        stage: String(stage || 'unknown').slice(0, 32),
        reason: String(reason == null ? '' : reason).slice(0, 500),
        question: String(state.askQuestion || (nlBox && nlBox.value) || '').slice(0, 500),
        generator_id: state.askGen != null ? Number(state.askGen) : (state.reportGen != null ? Number(state.reportGen) : null),
      }, extra || {});
      Object.keys(body).forEach(k => { if (body[k] == null || body[k] === '') delete body[k]; });
      fetch('/api/explore/client-error', { method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).catch(() => {});
    } catch (e) { /* the log must never take the answer down */ }
  }

  function endpointHost(cfg) {
    const ep = (cfg && cfg.endpoint) || '';
    const m = /^[a-z]+:\/\/([^/]+)/i.exec(ep);
    return m ? m[1] : (ep || t('provider'));
  }

  // Is there a model ANYWHERE this page can reach — here in the browser with
  // a key, or on the server? The whole one-shot design turns on this: with a
  // model, a form match is refined into a real cut and run; without one, the
  // Grand Total is the honest answer and the form is opened for the rest.
  function modelAvailable(cfg) {
    return !!((cfg && cfg.browser && browserKey(cfg)) || (cfg && cfg.server));
  }

  function readyKeys(gen) {
    return (gen.kpis || []).filter(k => k.ready).map(k => k.key);
  }

  // A truncated JSON form fails validation loudly; a truncated SELECT can
  // still parse as valid SQL and quietly answer the wrong question — so tier
  // 2 gets the bigger budget (mirrors nl2sql.MAX_SQL_TOKENS on the server).
  const MAX_FORM_TOKENS = 400;
  const MAX_SQL_TOKENS = 900;

  // One POST to the provider, from this page. Returns
  //   {raw}          the model's text
  //   {fallback:1}   could not reach the provider at all — try the server
  //   {reported:1}   the provider answered an error; the status line says so
  // Shared by tier 1 (a JSON form) and tier 2 (a SELECT): the only
  // difference is whether a schema rides along, and JSON mode with it.
  async function callProvider(cfg, prompt, schema, system, maxTokens) {
    try {
      const r = await fetch(String(cfg.endpoint).replace(/\/+$/, '') + '/chat/completions', {
        method: 'POST', signal: askSignal(),
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + browserKey(cfg) },
        body: JSON.stringify({
          model: cfg.model,
          messages: [
            { role: 'system', content: system
              || t('You respond with strictly one JSON object. No explanations and no markdown.') },
            { role: 'user', content: prompt + (schema
                ? t('\nrespond strictly in JSON per the schema: {p1}', {p1: JSON.stringify(schema)}) : '') },
          ],
          temperature: 0,
          max_tokens: maxTokens || MAX_FORM_TOKENS,
          response_format: schema ? { type: 'json_object' } : undefined,
        }),
      });
      if (!r.ok) {
        let detail = '';
        try { detail = JSON.stringify((await r.json()).error || '').slice(0, 200); } catch (e) {}
        nlStatus.textContent = (r.status === 401 || r.status === 402)
          ? t('Key {p1} rejected ({p2}). Check the key.', {p1: endpointHost(cfg), p2: r.status})
          : (t('{p1} replied {p2}. {p3}', {p1: endpointHost(cfg), p2: r.status, p3: detail}));
        reportClientError('provider_http', nlStatus.textContent, { status: r.status, host: endpointHost(cfg) });
        return { reported: 1 };
      }
      const data = await r.json();
      const raw = (((data.choices || [])[0] || {}).message || {}).content || '';
      if (!raw.trim()) {
        nlStatus.textContent = t('{p1} returned an empty response.', {p1: endpointHost(cfg)});
        reportClientError('provider_empty', nlStatus.textContent, { host: endpointHost(cfg) });
        return { reported: 1 };
      }
      return { raw: raw };
    } catch (e) {
      // A TypeError here is the CORS/network case: the browser refused or could
      // not make the request at all (blocked by the proxy, no CORS headers,
      // DNS). Say which host failed, then let the server try — where it has
      // egress, the question is still answered.
      nlStatus.textContent = t('The browser couldn\'t reach {p1}: {p2} — trying via the server…', {p1: endpointHost(cfg), p2: ((e && e.message) || e)});
      reportClientError('provider_network', (e && e.message) || String(e), { host: endpointHost(cfg) });
      return { fallback: 1 };
    }
  }

  // ── tier 1: the intent form ──────────────────────────────────────────
  // Every path below answers with ONE of these shapes, so the caller reads
  // like the decision tree it is:
  //   {kind:'intent'}  a validated intent — run it
  //   {kind:'form'}    the form matcher was confident, no model ran
  //   {kind:'none'}    "not one of the metrics" (422) — try tier 2
  //   {kind:'nomodel'} nothing to call (503 / no provider configured)
  //   {kind:'error'}   reported on the status line, nothing left to try
  // The confidence of a form match, in the ONE shape the chat reads:
  // `/api/explore/intent` calls it `match`, `/intent/prepare` returns the
  // raw `hit`. Both carry the same three numbers.
  // The signal every request of ONE question shares, so "Cancel" aborts
  // whichever of them is in flight. `undefined` (no controller, e.g. an
  // ancient browser) is exactly "no signal" to fetch.
  function askSignal() {
    return (state.askAbort && state.askAbort.signal) || undefined;
  }

  // The id of the row the SERVER wrote for the answer being emitted. A lake
  // answer records its own row on `lake/exec` (op `sql`, `final`) and that id
  // is the one 👍/👎 must carry; a tier-1 answer has only the intent route's.
  function answerIdOf(res) {
    return (res && res.answer_id) || state.askAnswerId || null;
  }

  function matchOf(data) {
    if (!data) return null;
    if (data.match) return data.match;
    const h = data.hit;
    if (!h) return null;
    return { score: h.score, runner_up: h.runner_up, form: h.form };
  }

  async function fetchIntent(question, gen, cfg, fixedKpi, prevIntent) {
    // Everything the router knew is kept for ONE consumer: the thread card's
    // tier badge and its list of alternatives. Set on every route so a
    // second question never inherits the first one's confidence.
    const keep = (out) => {
      state.askMatch = (out && out.match) || null;
      state.askShortlist = (out && out.shortlist) || null;
      // P4: the server's own id of the answer row it just wrote — the only
      // handle 👍/👎 has. Null on every route that recorded nothing.
      state.askAnswerId = (out && out.answer_id) || null;
      if (out && (out.kind === 'intent' || out.kind === 'form' || out.kind === 'why')) {
        emit('stage', { stage: 'intent', source: out.kind === 'form' ? 'form' : 'model' });
      }
      return out;
    };
    if (cfg.browser && browserKey(cfg)) {
      const out = await intentViaBrowser(question, gen, cfg, fixedKpi, prevIntent);
      if (out.kind !== 'fallback') return keep(out);
    }
    // stage 1 of the chat's pending card, "understanding the question", is done as soon
    // as a route answered — whichever route that was.
    // A fixed KPI means the server MUST call a model (there is no form
    // matching left to do); with no server provider that is a wasted round
    // trip, so say "no model" here rather than reading it off a 503.
    if (fixedKpi && !cfg.server) return keep({ kind: 'nomodel' });
    return keep(await intentViaServer(question, gen, fixedKpi, prevIntent));
  }

  // The previous answer's intent, trimmed to the five keys the server's own
  // validator accepts. Sent so a follow-up the CLIENT matcher could not settle
  // ("and what about the large clients?") still reaches the model with the context it
  // needs; the thread as a whole is never sent, and neither are any values the
  // server would not already have grounded itself.
  function prevIntentBody(prev) {
    if (!prev || !prev.kpi) return null;
    const out = { kpi: prev.kpi };
    if (prev.dims) out.dims = prev.dims;
    if (prev.filters) out.filters = prev.filters;
    if (prev.order) out.order = prev.order;
    if (prev.limit) out.limit = prev.limit;
    return out;
  }

  // prepare (no model) -> the provider, from this page -> validate.
  async function intentViaBrowser(question, gen, cfg, fixedKpi, prevIntent) {
    const body = { question: question, generator_id: gen.generator_id,
      ready_kpis: readyKeys(gen), fixed_kpi: fixedKpi || null,
      prev_intent: prevIntentBody(prevIntent) };
    const rp = await fetch('/api/explore/intent/prepare', {
      method: 'POST', signal: askSignal(), headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (rp.status === 422) return { kind: 'none' };
    if (!rp.ok) return { kind: 'fallback' };
    const prep = await rp.json();
    // A confident form match is answered by the server itself — no model,
    // no key, nothing leaves the company.
    if (prep.source === 'form') {
      return { kind: 'form',
        intent: Object.assign({}, prep.intent || {}, { probe_words: prep.residue || [] }),
        explanation: prep.explanation, match: matchOf(prep), shortlist: prep.shortlist || null,
        answer_id: prep.answer_id || null };
    }
    // A "why …?" the form matcher already settled: the metric is known,
    // no model is involved, and the answer is a card, not a row.
    if (prep.source === 'why' && prep.intent) {
      return { kind: 'why', intent: prep.intent, why: prep.why, explanation: prep.explanation,
        match: matchOf(prep), shortlist: prep.shortlist || null,
        answer_id: prep.answer_id || null };
    }
    const got = await callProvider(cfg, prep.prompt, prep.schema);
    if (got.fallback) return { kind: 'fallback' };
    if (got.reported) return { kind: 'error' };

    const rv = await fetch('/api/explore/intent/validate', {
      method: 'POST', signal: askSignal(), headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, generator_id: gen.generator_id,
        raw: got.raw, fixed_kpi: prep.fixed_kpi || fixedKpi || null,
        prev_intent: prevIntentBody(prevIntent) }),
    });
    if (rv.status === 422) return { kind: 'none' };
    if (!rv.ok) {
      let detail = '';
      try { detail = String((await rv.json()).detail || ''); } catch (e) {}
      nlStatus.textContent = detail || (t('Error ({p1}).', {p1: rv.status}));
      reportClientError('intent_validate', nlStatus.textContent, { status: rv.status, tier: 1 });
      return { kind: 'error' };
    }
    const data = await rv.json();
    const shortlist = data.shortlist || prep.shortlist || null;
    if (data.source === 'why') {
      return { kind: 'why', intent: data.intent || {}, why: data.why, explanation: data.explanation,
        match: matchOf(data), shortlist: shortlist, answer_id: data.answer_id || null };
    }
    return { kind: 'intent',
      intent: Object.assign({}, data.intent || {}, { probe_words: data.residue || [] }),
      explanation: data.explanation, match: matchOf(data), shortlist: shortlist,
      answer_id: data.answer_id || null };
  }

  async function intentViaServer(question, gen, fixedKpi, prevIntent) {
    const r = await fetch('/api/explore/intent', {
      method: 'POST', signal: askSignal(), headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, generator_id: gen.generator_id,
        ready_kpis: readyKeys(gen), fixed_kpi: fixedKpi || null,
        prev_intent: prevIntentBody(prevIntent) }),
    });
    if (r.status === 422) return { kind: 'none' };
    if (r.status === 503) return { kind: 'nomodel' };
    if (!r.ok) {
      let detail = '';
      try { detail = String((await r.json()).detail || ''); } catch (e) {}
      nlStatus.textContent = detail || (t('Error ({p1}).', {p1: r.status}));
      reportClientError('intent_server', nlStatus.textContent, { status: r.status, tier: 1 });
      return { kind: 'error' };
    }
    const data = await r.json();
    if (data.source === 'form') {
      return { kind: 'form',
        intent: Object.assign({}, data.intent || {}, { probe_words: data.residue || [] }),
        explanation: data.explanation, match: matchOf(data), shortlist: data.shortlist || null,
        answer_id: data.answer_id || null };
    }
    if (data.source === 'why') {
      return { kind: 'why', intent: data.intent || {}, why: data.why, explanation: data.explanation,
        match: matchOf(data), shortlist: data.shortlist || null,
        answer_id: data.answer_id || null };
    }
    return { kind: 'intent',
      intent: Object.assign({}, data.intent || {}, { probe_words: data.residue || [] }),
      explanation: data.explanation, match: matchOf(data), shortlist: data.shortlist || null,
      answer_id: data.answer_id || null };
  }

  // ── Verifier: the model re-reads its own homework ────────────────────
  // One advisory call between "intent assembled" and "number shown": does the
  // structured query answer the question, and is the output format right?
  // Fail-open everywhere — null (silence) on any error, timeout or absence
  // of a model; only an explicit {"ok": false} holds the number back, and
  // even then "Calculate anyway" is one click away.
  function parseVerdictLocal(raw) {
    const m = String(raw || '').match(/\{[\s\S]*\}/);
    if (!m) return null;
    try {
      const o = JSON.parse(m[0]);
      if (!o || o.ok !== false) return null;
      return { ok: false, issue: String(o.issue || '').slice(0, 300),
               question: String(o.question || '').slice(0, 300) };
    } catch (e) { return null; }
  }
  async function verifyIntent(question, gen, intent, cfg) {
    if (!modelAvailable(cfg)) return null;
    const body = { question: question, generator_id: gen.generator_id,
      intent: { kpi: intent.kpi, dims: intent.dims || [], filters: intent.filters || [] },
      residue: intent.probe_words || [] };
    const work = (async () => {
      if (cfg.browser && browserKey(cfg)) {
        const rp = await fetch('/api/explore/intent/verify/prepare', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body) });
        if (!rp.ok) return null;
        const prep = await rp.json();
        const got = await callProvider(cfg, prep.prompt, prep.schema);
        return got && got.raw ? parseVerdictLocal(got.raw) : null;
      }
      if (cfg.server) {
        const r = await fetch('/api/explore/intent/verify', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body) });
        if (!r.ok) return null;
        const v = await r.json();
        return v && v.ok === false ? v : null;
      }
      return null;
    })();
    let timer = null;
    try {
      return await Promise.race([work,
        new Promise(res => { timer = setTimeout(() => res(null), 20000); })]);
    } catch (e) {
      return null;
    } finally {
      if (timer) clearTimeout(timer);   // a live 20 s timer keeps node tests from exiting
    }
  }
  // `pending` = {intent, gen, question, explanation, via}: the intent the
  // controller is holding. The chat needs it because the operator's next
  // line («2026») answers the controller, not the previous open answer —
  // prod 2026-08-28: that reply re-ran the Bristol week instead.
  function renderVerify(verdict, proceed, pending) {
    const go = () => { clearClarify(); proceed(); };
    const resume = (filters) => {
      clearClarify();
      if (!pending || !pending.intent) { proceed(); return; }
      state.askVia = pending.via || 'model';
      const intent = Object.assign({}, pending.intent, { filters: filters || pending.intent.filters || [] });
      return finishIntent(withGen(intent, pending.gen), pending.gen, pending.explanation);
    };
    if (clarifyBox) {
      clarifyBox.textContent = '';
      const line = document.createElement('div');
      line.className = 'ex-clarify-line';
      const lbl = document.createElement('span');
      lbl.className = 'ex-clarify-word';
      lbl.textContent = verdict.question || verdict.issue || t('The query may not answer the question.');
      line.appendChild(lbl);
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ex-clarify-chip';
      b.textContent = t('calculate anyway');
      b.addEventListener('click', go);
      line.appendChild(b);
      clarifyBox.appendChild(line);
      clarifyBox.hidden = false;
    }
    nlStatus.textContent = t('Verifier: {p1} — clarify the question or click "Calculate anyway".', {p1: (verdict.issue || t('the query may not answer the question'))});
    emit('verify', { issue: verdict.issue || '', question: verdict.question || '', proceed: go,
      resume: resume, intent: pending ? pending.intent : null, gen: pending ? pending.gen : null,
      asked: pending ? pending.question : '' });
  }

  // ── Clarify chips: the engine found a question word in TWO dims ──────
  // One round by construction: a click consumes the word (it leaves
  // `probe_words`), so the next pass has nothing left to ask about.
  const clarifyBox = document.getElementById('ex-clarify');
  function clearClarify() {
    if (clarifyBox) { clarifyBox.hidden = true; clarifyBox.textContent = ''; }
    emit('clarify', null);
  }
  function renderClarify(clarify, intent, gen, explanation, note) {
    const resolve = (word, dim, value) => {
      const next = Object.assign({}, intent, {
        probe_words: (intent.probe_words || []).filter(w => w !== word),
        filters: dim ? (intent.filters || []).concat([{ dim: dim, op: '=', value: value }])
                     : (intent.filters || []),
      });
      clearClarify();
      finishIntent(next, gen, explanation, note);
    };
    if (clarifyBox) {
      clarifyBox.textContent = '';
      clarify.forEach(c => {
        const line = document.createElement('div');
        line.className = 'ex-clarify-line';
        const lbl = document.createElement('span');
        lbl.className = 'ex-clarify-word';
        lbl.textContent = t('"{p1}" is:', {p1: c.word});
        line.appendChild(lbl);
        (c.options || []).forEach(o => {
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'ex-clarify-chip';
          b.textContent = o.dim + ' = ' + o.value;
          b.addEventListener('click', () => resolve(c.word, o.dim, o.value));
          line.appendChild(b);
        });
        const skip = document.createElement('button');
        skip.type = 'button';
        skip.className = 'ex-clarify-chip ex-clarify-skip';
        skip.textContent = t('doesn\'t matter, show without it');
        skip.addEventListener('click', () => resolve(c.word, null, null));
        line.appendChild(skip);
        clarifyBox.appendChild(line);
      });
      clarifyBox.hidden = false;
    }
    nlStatus.textContent = t('Clarification needed: the word from the question appears in several dimensions.');
    // The chat pane renders its own bubble from this — with the SAME resolve.
    emit('clarify', { question: state.askQuestion || '', clarify: clarify, resolve: resolve });
  }

  // The shared tail of every tier-1 path: the worker turns the validated
  // intent into DETERMINISTIC SQL (from the report's own frozen definition,
  // never a model's text), the SQL is shown AND run. That invariant is what
  // makes auto-running safe here and is why tier 2 is labelled differently.
  async function finishIntent(intent, gen, explanation, note) {
    // A NEW question starts here — the follow-up chips, the clarify options
    // and "Recalculate" never go through ask(), so this is the only place
    // that can clear a cancel flag left over from the previous one.
    state.askCancelled = false;
    // Not the 10 s used elsewhere for chatter: a timeout here would
    // terminate the worker and throw away the whole index for a string.
    const out = await send('explore:intent', { intent: intent }, HARD_QUERY_TIMEOUT_MS);
    if (out && out.sql) emit('stage', { stage: 'sql' });
    if (!out || out.error) {
      nlStatus.textContent = (out && out.error) || t('Failed to build the query.');
      return true;
    }
    // The engine could not settle a word of the question against the data
    // (one value living in two dims): a NUMBER here would answer a question
    // nobody asked. Chips instead — the click re-enters this very function.
    if (out.clarify && out.clarify.length) {
      renderClarify(out.clarify, intent, gen, explanation, note);
      return true;
    }
    clearClarify();
    state.lastNotes = (out.notes || []).slice();
    sqlBox.value = out.sql || '';
    // The engine may have FIXED a filter against the data ("Grando is a
    // shipping_group, not a shipping_display") — say so, the user must see it.
    const engineNotes = (out.notes || []).join(' · ');
    const status = intentStatus(out.kpi || {}, gen, intent, explanation,
      [note, engineNotes].filter(Boolean).join(' · '));
    nlStatus.textContent = status;
    // Deterministic SQL (built by the engine from the report's own frozen
    // definition) — the operator has nothing to verify, so the SQL row stays
    // collapsed. Only model-written SQL opens it.
    setSqlRowOpen(false);
    primeAnswer(firstClause(status), Object.assign({}, kpiOf(gen, intent.kpi), out.kpi || {}), intent);
    await runQuery();
    return true;
  }

  // ── "Why …?" — the card, not a single number ─────────────────────
  // The intent form cannot express a reason, and pretending it can produced
  // the defect this feature exists for: a superlative rule bolted an
  // `ORDER BY value DESC LIMIT 1` onto a one-row aggregate and called it an
  // answer. So a why-question is answered with the three deterministic
  // numbers that actually start an investigation:
  //
  //   a. the metric for the period the question named,
  //   b. the same metric one period earlier (shiftPeriod) + the delta,
  //   c. where it is concentrated — top-3 by each non-time dim the KPI has.
  //
  // Every one of those is an `explore:intent` → `explore:sql` pair over the
  // report's OWN frozen definition, run through the worker STRICTLY
  // sequentially (the worker is single-threaded; parallel sends would just
  // queue behind each other while sharing one hard timeout).
  const WHY_MAX_DIMS = 4;
  const WHY_TOP = 3;

  function kpiOf(gen, key) {
    return ((gen && gen.kpis) || []).find(k => k.key === key) || {};
  }

  // ── "Trend …" — two periods per cut and the delta ────────────────────
  // Deterministic: the KPI's own cell SQL cut by [dim, period], the last
  // `spec.n` periods PRESENT IN THE DATA (not on the calendar — "the last
  // two months" means the two newest months the folder has), then per cut
  // first → last and Δ, worst first. Nothing is sent to a model here.
  async function trendCard(intent, gen, question, spec) {
    const kpi = kpiOf(gen, intent.kpi);
    const kdims = kpi.dims || [];
    const isTime = d => Object.prototype.hasOwnProperty.call(TIME_DIMS, d);
    const dim = (intent.dims || []).find(d => !isTime(d)) || kdims.find(d => !isTime(d)) || null;
    const gran = kdims.includes(spec.gran) ? spec.gran : (['month', 'week', 'day', 'year'].find(g => kdims.includes(g)) || null);
    if (!gran) {
      nlStatus.textContent = t('Metric "{p1}" has no period dimension — its trend cannot be calculated.', {p1: (kpi.short || kpi.name || intent.kpi)});
      return true;
    }
    const base = withGen({ kpi: intent.kpi, dims: [dim, gran].filter(Boolean),
      filters: (intent.filters || []).filter(f => f && !isTime(f.dim)), order: null, limit: 5000 }, gen);
    nlStatus.textContent = t('Calculating the trend…');
    const built = await send('explore:intent', { intent: base }, HARD_QUERY_TIMEOUT_MS);
    if (!built || built.error) { nlStatus.textContent = (built && built.error) || t('Failed to build the query.'); reportClientError('trend_build', nlStatus.textContent, { tier: 1 }); return true; }
    sqlBox.value = built.sql || '';
    setSqlRowOpen(false);
    const qForRecord = state.askQuestion || question;
    const res = await send('explore:sql',
      { sql: built.sql, limit: 5000, final: true, question: qForRecord },
      HARD_QUERY_TIMEOUT_MS);
    if (!res || res.error) { nlStatus.textContent = (res && res.error) || t('Failed to run the query.'); reportClientError('trend_query', nlStatus.textContent, { tier: 1, sql: String(built.sql || '').slice(0, 400) }); return true; }
    const cols = res.columns || [];
    const gi = cols.indexOf(gran), di = dim ? cols.indexOf(dim) : -1, vi = cols.indexOf('value');
    if (gi < 0 || vi < 0) { nlStatus.textContent = t('Result without a period or value — the trend cannot be calculated.'); return true; }
    // Periods order numerically when they are numbers (month 7 < 8) and
    // lexically otherwise (ISO dates, 'W31.2026'-style labels).
    const cmp = (a, b) => (isFinite(Number(a)) && isFinite(Number(b))) ? Number(a) - Number(b) : String(a).localeCompare(String(b));
    const periods = Array.from(new Set((res.rows || []).map(r => r[gi]).filter(v => v != null).map(String)))
      .sort(cmp).slice(-spec.n);
    if (periods.length < 2) {
      nlStatus.textContent = t('There are fewer than two periods of "{p1}" in the data — there\'s nothing to compare the trend with.', {p1: TIME_DIMS[gran]});
      return true;
    }
    const first = periods[0], last = periods[periods.length - 1];
    // A date period prints as a date, not as a midnight timestamp.
    const pf = v => String(v).replace(/[T ]00:00:00(\.0+)?$/, '');
    const byCut = new Map();
    (res.rows || []).forEach(r => {
      const key = di >= 0 ? String(r[di] == null ? '' : r[di]) : '∑';
      if (!byCut.has(key)) byCut.set(key, {});
      byCut.get(key)[String(r[gi])] = Number(r[vi]);
    });
    const rows = [];
    byCut.forEach((m, key) => {
      const a = m[String(first)], b = m[String(last)];
      const d = (isFinite(a) && isFinite(b)) ? b - a : null;
      rows.push([key, isFinite(a) ? a : null, isFinite(b) ? b : null, d]);
    });
    rows.sort((x, y) => (x[3] == null) - (y[3] == null) || (x[3] - y[3]));
    const label = kpi.short || kpi.name || intent.kpi;
    const pl = TIME_DIMS[gran].toLowerCase();
    const title = t('Trend "{p1}"{p2}: {p3} {p4} → {p5}', {p1: label, p2: (dim ? t(' by {p1}', {p1: dim}) : ''), p3: pl, p4: pf(first), p5: pf(last)});
    const shown = { columns: [dim || t('total'), pl + ' ' + pf(first), pl + ' ' + pf(last), 'Δ'], rows: rows,
      total: rows.length, ms: res.ms, partitions_used: res.partitions_used || [],
      partitions_expected: res.partitions_expected, missing_partitions: res.missing_partitions || [] };
    state.lastIntent = base; state.lastKpi = kpi; state.lastSql = built.sql;
    state.lastAnswer = { title: title, sql: built.sql };
    setAnswerTitle(title);
    // The table shows values in the metric's terms; the record keeps numbers.
    renderResult(Object.assign({}, shown, { rows: rows.map(r => [r[0], fmtValue(r[1], kpi), fmtValue(r[2], kpi), r[3] == null ? '' : fmtValue(r[3], kpi)]) }));
    renderHow(res);
    const worst = rows.find(r => r[3] != null && r[3] < 0);
    nlStatus.textContent = worst
      ? t('The largest decrease: {p1} ({p2} → {p3}, Δ {p4})', {p1: worst[0], p2: fmtValue(worst[1], kpi), p3: fmtValue(worst[2], kpi), p4: fmtValue(worst[3], kpi)})
      : t('There is no decrease in any dimension for {p1} {p2} → {p3}.', {p1: pl, p2: pf(first), p3: pf(last)});
    const q = state.askQuestion || question;
    const src = state.askSource || state.askVia || 'ask';
    state.askQuestion = null; state.askSource = null; state.askVia = null;
    state.openAnswer = { question: q, intent: base, kpi: kpi, sql: built.sql, kind: 'trend' };
    emit('answer', { question: q, intent: base, kpi: kpi, sql: built.sql, res: shown, kind: 'trend', source: src,
      parent_n: state.askParentN || null, match: state.askMatch, shortlist: state.askShortlist,
      // `shown` is built here and never carries an id; `res` is the answer
      // the SERVER ran, so a lake row's own id is the one 👍/👎 must go to
      // (see `answerIdOf`).
      regen: state.askRegen, answer_id: answerIdOf(res),
      trend: { dim: dim, gran: gran, first: first, last: last } });
    state.askAnswerId = null;
    state.askMatch = null; state.askShortlist = null; state.askRegen = false;
    state.askParentN = null;
    return true;
  }

  // One sub-query of the card: build the deterministic SQL, then run it.
  // Never throws — an error is data, because a card that computed two of its
  // three numbers must still show the two.
  // `opts.record` (a question string) marks THIS sub-query as the one that
  // stands for the whole card in explore_answers — used only by whyCard's
  // headline (current-period) call. Every other caller (prev-period,
  // per-dim concentration, grandTotals' background loop) omits it, so the
  // server never sees a row for those — one why-card, one recorded answer.
  async function whyRun(intent, opts) {
    const built = await send('explore:intent', { intent: intent }, HARD_QUERY_TIMEOUT_MS);
    if (!built || built.error) return { error: (built && built.error) || t('Failed to build the query.') };
    const record = opts && opts.record;
    const res = await send('explore:sql',
      record ? { sql: built.sql, limit: 1000, final: true, question: record }
             : { sql: built.sql, limit: 1000 },
      HARD_QUERY_TIMEOUT_MS);
    if (!res || res.error) return { sql: built.sql, error: (res && res.error) || t('The query didn\'t run.') };
    return { sql: built.sql, res: res, kpi: built.kpi || {} };
  }

  // The metric's value out of a result: the engine always emits a `value`
  // column (intent_sql), and for a ratio KPI `n`/`d` beside it.
  function whyValue(res) {
    const cols = (res && res.columns) || [];
    const row = ((res && res.rows) || [])[0];
    if (!row) return null;
    const iv = cols.indexOf('value');
    if (iv >= 0 && row[iv] != null) return Number(row[iv]);
    const inn = cols.indexOf('n'), idd = cols.indexOf('d');
    if (inn >= 0 && idd >= 0 && Number(row[idd])) return Number(row[inn]) / Number(row[idd]);
    return null;
  }

  function whyNum(v) {
    if (v == null || typeof v !== 'number' || !isFinite(v)) return '—';
    const a = Math.abs(v);
    return String(a < 10 ? Math.round(v * 1000) / 1000 : Math.round(v * 100) / 100);
  }

  function whyDelta(cur, prev) {
    if (cur == null || prev == null || !isFinite(cur) || !isFinite(prev)) return '—';
    const d = cur - prev;
    return (d > 0 ? '+' : '') + whyNum(d);
  }

  async function whyCard(intent, gen, why) {
    state.askCancelled = false;   // a new question (see finishIntent)
    const kpi = kpiOf(gen, intent.kpi);
    const label = kpi.name || kpi.short || intent.kpi || '?';
    const period = (why && why.period) || intent.filters || [];
    const base = { kpi: intent.kpi, generator_id: gen.generator_id, dims: [], filters: period };
    const problems = [];

    nlStatus.textContent = t('Why: calculating "{p1}"…', {p1: label});
    // The exact bubble text (firstClause(text)) isn't known until after this
    // call answers — a typed question or the metric label is the honest
    // stand-in, same fallback order the bubble itself falls back to below.
    const qForRecord = state.askQuestion || (nlBox && nlBox.value.trim())
      || (t('Why: {p1}{p2}', {p1: label, p2: (periodLabel(period) ? t(' for {p1}', {p1: periodLabel(period)}) : '')}));
    const cur = await whyRun(base, { record: qForRecord });
    if (cur.error) {
      nlStatus.textContent = t('Why: "{p1}" — {p2}', {p1: label, p2: cur.error});
      return true;
    }
    // The current-period SQL is the one "SQL of this answer" must show: it is
    // the query the headline number came from.
    sqlBox.value = cur.sql || '';
    state.lastSql = cur.sql || '';
    const curVal = whyValue(cur.res);

    let prevVal = null;
    const prevFilters = shiftPeriod(period);
    if (prevFilters) {
      const prev = await whyRun(Object.assign({}, base, { filters: prevFilters }));
      if (prev.error) problems.push(t('previous period: {p1}', {p1: prev.error}));
      else prevVal = whyValue(prev.res);
    }

    // c. concentration: top-3 by each non-time dim the KPI actually has.
    const dims = (kpi.dims || []).filter(d => !(d in TIME_DIMS)).slice(0, WHY_MAX_DIMS);
    const columns = [t('dimension'), t('Value'), 'value'];
    let ratio = false;
    const rows = [];
    for (const d of dims) {
      const sub = await whyRun(Object.assign({}, base, {
        dims: [d], order: { by: 'value', dir: 'desc' }, limit: WHY_TOP }));
      if (sub.error) { problems.push(t('dimension {p1}: {p2}', {p1: d, p2: sub.error})); continue; }
      const cols = (sub.res.columns || []);
      const iDim = cols.indexOf(d), iVal = cols.indexOf('value');
      const iN = cols.indexOf('n'), iD = cols.indexOf('d');
      if (iN >= 0 && iD >= 0) ratio = true;
      (sub.res.rows || []).forEach(r => {
        rows.push([d, iDim >= 0 ? r[iDim] : '', iVal >= 0 ? r[iVal] : null,
          iN >= 0 ? r[iN] : null, iD >= 0 ? r[iD] : null]);
      });
    }

    const pl = periodLabel(period);
    let text = t('Why: {p1}{p2} = {p3}', {p1: label, p2: (pl ? t(' for {p1}', {p1: pl}) : ''), p3: whyNum(curVal)});
    if (prevFilters) {
      text += t(' (previous period {p1}; Δ {p2})', {p1: whyNum(prevVal), p2: whyDelta(curVal, prevVal)});
    } else {
      text += t(' (no period specified — nothing to compare)');
    }
    if (rows.length) text += t(' · where it\'s concentrated (top {p1} by {p2})', {p1: WHY_TOP, p2: dims.join(', ')});
    if (problems.length) text += t(' · not calculated: {p1}', {p1: problems.join('; ')});
    nlStatus.textContent = text;
    // The card is deterministic too — three intent runs over the frozen
    // definition — so the SQL row stays closed and the answer card names the
    // metric and the period, which is the card's own first sentence.
    setSqlRowOpen(false);
    state.lastKpi = kpi;
    state.lastIntent = base;
    state.lastAnswer = { title: firstClause(text), sql: state.lastSql };
    setAnswerTitle(firstClause(text));

    // The grid carries the concentration rows (the part of the card with
    // more than one number in it). With no non-time dim to cut by there is
    // nothing to concentrate — the period's own result is the honest grid.
    state.sortCol = null; state.sortDir = 1;
    let shown;
    if (rows.length) {
      shown = {
        columns: ratio ? columns.concat(['n', 'd']) : columns,
        rows: ratio ? rows : rows.map(r => r.slice(0, 3)),
        total: rows.length, ms: cur.res.ms,
        partitions_used: cur.res.partitions_used || [],
        partitions_expected: cur.res.partitions_expected,
        missing_partitions: cur.res.missing_partitions || [],
        truncated: false, incomplete: !!cur.res.incomplete,
      };
      renderResult(shown);
      // The concentration grid has no `n/d/value` first row to explain, but
      // the current-period result does — "how it's calculated" is about the
      // headline number, so it is fed that one.
      renderHow(cur.res);
    } else {
      shown = cur.res;
      renderResult(shown);
      renderHow(cur.res);
    }
    // A why-answer never goes through runQuery, so it emits its own event —
    // with the three numbers the card is actually about, so the bubble can
    // render the same three lines without recomputing anything.
    const q = state.askQuestion || (nlBox && nlBox.value.trim()) || firstClause(text);
    const src = state.askSource || state.askVia || 'ask';
    const parentN = state.askParentN || null;
    state.askQuestion = null; state.askSource = null; state.askVia = null; state.askParentN = null;
    state.openAnswer = { question: q, intent: base, kpi: kpi, sql: state.lastSql, kind: 'why' };
    emit('answer', { question: q, intent: base, kpi: kpi, sql: state.lastSql,
      res: shown, kind: 'why', source: src, parent_n: parentN,
      match: state.askMatch, shortlist: state.askShortlist, regen: state.askRegen,
      // `cur.res` — the headline sub-query, the one the server recorded for
      // this card (`whyRun`'s `record`); `shown` is assembled locally.
      answer_id: answerIdOf(cur.res),
      why: { label: label, period: pl, current: curVal, previous: prevVal,
             delta: whyDelta(curVal, prevVal), shifted: !!prevFilters } });
    state.askMatch = null; state.askShortlist = null; state.askRegen = false;
    state.askAnswerId = null;
    return true;
  }

  // ── Phase 4.5: "why is the number in the report 25.5?" asked COLD ──────────────
  // No open answer, no metric named — only a number. The model cannot help
  // here by construction: cell values never reach the server, so «25,5» is a
  // string it has never seen. The data is in THIS browser's engine, so the
  // lookup runs here.
  //
  // NOTHING on this route reaches the server — not the SQL, not even the
  // question text, so this answer leaves no `explore_intent` audit entry.
  // That is the same trade the deterministic metrics form already makes: a
  // route built entirely from the report's own frozen definitions has nothing
  // to send, and adding a POST purely to log it would put question text on the
  // wire for a path that deliberately keeps it off.
  const VALUE_MAX_KPIS = 20;

  const RU_MONTH_STEMS = [['январ', 1], ['феврал', 2], ['март', 3], ['апрел', 4],
    ['ма', 5], ['июн', 6], ['июл', 7], ['август', 8], ['сентябр', 9],
    ['октябр', 10], ['ноябр', 11], ['декабр', 12]];

  function askWords(text) {
    return String(text == null ? '' : text).toLowerCase().replace(/ё/g, 'е')
      .split(/[^a-zа-я0-9]+/).filter(Boolean);
  }

  // The period the question NAMES — the year/month half of nl2sql's
  // `_question_times`. Ambiguity ("June or July") yields nothing, exactly as
  // on the server: neither month is what was asked.
  function questionPeriod(question) {
    const toks = askWords(question);
    const out = [];
    const years = Array.from(new Set(toks.filter(tv => /^20\d\d$/.test(tv)).map(Number)));
    if (years.length === 1) out.push({ dim: 'year', op: '=', value: years[0] });
    const months = new Set();
    toks.forEach(tv => RU_MONTH_STEMS.forEach(([stem, num]) => {
      if (stem === 'ма') { if (/^ма[яе]$/.test(tv)) months.add(5); return; }
      if (tv.indexOf(stem) === 0) months.add(num);
    }));
    if (months.size === 1) out.push({ dim: 'month', op: '=', value: months.values().next().value });
    return out;
  }

  // Does the question already name a metric confidently? Then it is NOT the
  // cold case and the existing route (server classifier / form matcher) owns
  // it. This is a client mirror of `nl2sql.match_forms`' confidence rule
  // (best ≥ 2 shared stems AND ≥ 2× the runner-up) used ONLY as a veto: a
  // false negative costs a value lookup, never a wrong answer.
  const ASK_STOP = new Set(['почему', 'зачем', 'отчего', 'что', 'как', 'какой', 'какая',
    'какое', 'какие', 'сколько', 'где', 'когда', 'за', 'по', 'в', 'во', 'на', 'и', 'или',
    'у', 'с', 'из', 'до', 'от', 'это', 'отчет', 'отчете', 'число', 'числе', 'вдруг', 'там']);
  function askStem(w) { return w.length >= 4 ? w.slice(0, 4) : w; }
  function askKeys(text) {
    const out = new Set();
    askWords(text).forEach(w => {
      if (w.length < 2 || ASK_STOP.has(w) || /^\d/.test(w)) return;
      out.add(askStem(w));
    });
    return out;
  }
  function clientFormMatch(question, gen) {
    const qk = askKeys(question);
    if (!qk.size) return null;
    const scored = ((gen && gen.kpis) || []).filter(k => k.ready).map(k => {
      const kk = askKeys([k.name, k.short].concat(k.question_forms || []).join(' '));
      let score = 0;
      qk.forEach(s => { if (kk.has(s)) score += 1; });
      return { kpi: k, score: score };
    }).sort((a, b) => b.score - a.score);
    if (!scored.length || scored[0].score < 2) return null;
    const runnerUp = scored.length > 1 ? scored[1].score : 0;
    return scored[0].score >= 2 * Math.max(runnerUp, 1) ? scored[0].kpi : null;
  }

  function intentSig(intent) {
    return JSON.stringify([String(intent.kpi), (intent.dims || []).slice().sort(),
      (intent.filters || []).map(f => [String(f.dim), String(f.op || '='), String(f.value)]).sort()]);
  }

  function valueChipLabel(gen, intent) {
    const k = kpiOf(gen, intent.kpi);
    const cut = (intent.dims || []).length ? (intent.dims || []).join(', ') : t('total');
    return t('This is about: {p1} · {p2} · {p3}', {p1: (k.short || k.name || intent.kpi), p2: cut, p3: (periodLabel(intent.filters) || t('no period'))});
  }

  async function historyRecords(gen) {
    const out = [];
    // The open answer is the cheapest record of all — it is already on screen.
    const open = state.openAnswer;
    if (open && open.intent && open.intent.kpi && state.lastResult) {
      out.push({ intent: open.intent, rows: state.lastResult.rows || [] });
    }
    if (typeof state.historyProvider === 'function') {
      try {
        const recs = await state.historyProvider(gen);
        (recs || []).forEach(r => out.push(r));
      } catch (e) { /* a thread that cannot be read is simply not searched */ }
    }
    return out;
  }

  // The Grand Total of every ready KPI, sequentially (the worker is
  // single-threaded — parallel sends only queue behind each other while
  // sharing one hard timeout), cached per index generation + period.
  async function grandTotals(gen, period) {
    const key = String(gen.generator_id) + '|' + state.indexGen + '|' + JSON.stringify(period);
    if (state.gtCache && state.gtCache.key === key) return state.gtCache.list;
    const kpis = (gen.kpis || []).filter(k => k.ready).slice(0, VALUE_MAX_KPIS);
    const list = [];
    for (const k of kpis) {
      const filters = (period || []).filter(f => (k.dims || []).indexOf(f.dim) >= 0);
      const intent = { kpi: k.key, dims: [], filters: filters };
      const out = await whyRun(Object.assign({ generator_id: gen.generator_id }, intent));
      if (out.error) continue;
      const v = whyValue(out.res);
      if (v == null) continue;
      list.push({ value: v, intent: intent, kpi: k });
    }
    state.gtCache = { key: key, list: list };
    return list;
  }

  // Cheapest first, and it stops as soon as a layer produced anything: an
  // answer already on screen beats re-running twenty metrics.
  async function valueLookup(question, gen, parsed) {
    const cands = [];
    const seen = new Set();
    const push = (intent) => {
      const sig = intentSig(intent);
      if (seen.has(sig)) return;
      seen.add(sig);
      cands.push({ intent: intent, label: valueChipLabel(gen, intent) });
    };
    (await historyRecords(gen)).forEach(rec => {
      if (!rec || !rec.intent || !rec.intent.kpi) return;
      if (!scanRowsForValue(rec.rows, parsed, !!(rec.kpi && rec.kpi.measure === 'ratio'))) return;
      push({ kpi: rec.intent.kpi, dims: [], filters: (rec.intent.filters || []).slice() });
    });
    if (cands.length) return cands;
    (await grandTotals(gen, questionPeriod(question))).forEach(g => {
      // Both readings of a ratio KPI are tried inside `numberMatches`
      // (0,255 displays as «25,5 %»).
      if (numberMatches(g.value, parsed, !!(g.kpi && g.kpi.measure === 'ratio'))) push(g.intent);
    });
    return cands;
  }

  function clearValueChips() {
    if (!valueChips) return;
    valueChips.textContent = '';
    valueChips.hidden = true;
  }

  function renderValueChips(items) {
    if (!valueChips) return;
    valueChips.textContent = '';
    items.forEach(it => {
      const btn = el('button', { type: 'button', class: 'ex-recipe-btn',
        'data-kpi': it.intent ? String(it.intent.kpi) : '', text: it.label });
      btn.addEventListener('click', () => { clearValueChips(); it.run(); });
      valueChips.appendChild(btn);
    });
    valueChips.hidden = !items.length;
  }

  // Returns true when this question WAS the cold "why <number>?" case and
  // has been answered (or honestly declined) here. False = not this route's
  // business; the caller's existing routing runs untouched.
  async function askValueRoute(question, gen) {
    if (!valueChips) return false;                       // no container, no route
    if (!isWhyQuestion(question)) return false;          // regression guard #1
    const parsed = parseAskedNumber(question);
    if (!parsed) return false;                           // a why-question without a number
    // A previous answer is open: the existing follow-up/why routing already
    // knows which metric is meant, and it is a better answer than a search.
    if (state.openAnswer && state.openAnswer.intent && state.openAnswer.intent.kpi) return false;
    if (clientFormMatch(question, gen)) return false;    // the question names a metric
    clearValueChips();
    nlStatus.textContent = t('Looking for the number {p1} in the report data…', {p1: parsed.raw});
    const hits = await valueLookup(question, gen, parsed);
    if (hits.length === 1) {
      const intent = hits[0].intent;
      state.askQuestion = question;
      await whyCard(withGen(intent, gen), gen, { period: intent.filters || [] });
      return true;
    }
    if (hits.length > 1) {
      renderValueChips(hits.map(h => ({
        intent: h.intent, label: h.label,
        run: () => { state.askQuestion = question;
          whyCard(withGen(h.intent, gen), gen, { period: h.intent.filters || [] }); },
      })));
      nlStatus.textContent = t('The number {p1} appears in several metrics — select which one you mean', {p1: parsed.raw});
      return true;
    }
    // Honest: the number is not in what the engine holds right now. The
    // metric chips are the way out that needs no model at all.
    const period = questionPeriod(question);
    renderValueChips(((gen.kpis || []).filter(k => k.ready).slice(0, VALUE_MAX_KPIS)).map(k => {
      const intent = { kpi: k.key, dims: [],
        filters: period.filter(f => (k.dims || []).indexOf(f.dim) >= 0) };
      return { intent: intent, label: k.short || k.name || k.key,
        run: () => { state.askQuestion = question;
          whyCard(withGen(intent, gen), gen, { period: intent.filters }); } };
    }));
    nlStatus.textContent = t('The number {p1} was not found in the current data — clarify the metric', {p1: parsed.raw});
    return true;
  }

  const MONTH_NAMES = [t('January'), t('February'), t('March'), t('April'), t('May'), t('June'),
    t('July'), t('August'), t('September'), t('October'), t('November'), t('December')];

  // "month: July, year: 2026" from the intent's filters — what the operator needs
  // to see to trust the number without reading the SQL.
  function periodLabel(filters) {
    const parts = [];
    (filters || []).forEach(f => {
      if (!f || !(f.dim in TIME_DIMS)) return;
      const v = f.op === 'in' ? (f.values || []).join(', ') : f.value;
      if (v == null || v === '') return;
      if (f.dim === 'month' && f.op === '=' && MONTH_NAMES[Number(v) - 1]) {
        parts.push(MONTH_NAMES[Number(v) - 1]);
        return;
      }
      parts.push(TIME_DIMS[f.dim].toLowerCase() + ' ' + v);
    });
    return parts.join(', ');
  }

  function intentStatus(k, gen, intent, explanation, note) {
    const dims = (intent.dims || []);
    let text = t('Answer for metric "{p1}" ({p2}) — dimension: {p3}', {p1: (k.name || k.key || intent.kpi || '?'), p2: gen.generator, p3: (dims.length ? dims.join(', ') : t('total across the whole metric'))});
    const period = periodLabel(intent.filters);
    if (period) text += t(', period: {p1}', {p1: period});
    if (explanation) text += ' · ' + explanation;
    if (note) text += ' · ' + note;
    return text;
  }

  // A confident form match, one shot. With a model around, ask it for the CUT
  // only (`fixed_kpi`: the metric is settled, the model may not change it) and
  // run. With no model at all, the Grand Total IS the answer to "what was SL in
  // July" — run it immediately, and open the metric's form prefilled so the
  // next question ("and by chain?") is one click, not another round trip.
  async function answerFormMatch(intent, gen, question, cfg, explanation, prevIntent) {
    if (modelAvailable(cfg)) {
      const cut = await fetchIntent(question, gen, cfg, intent.kpi, prevIntent);
      if (cut.kind === 'intent') {
        const flag = await verifyIntent(question, gen, cut.intent, cfg);
        if (flag) {
          renderVerify(flag, () => { state.askVia = 'form';
            finishIntent(withGen(cut.intent, gen), gen, cut.explanation || explanation); },
            { intent: cut.intent, gen: gen, question: question, explanation: cut.explanation || explanation, via: 'form' });
          return true;
        }
        return finishIntent(withGen(cut.intent, gen), gen, cut.explanation || explanation);
      }
      if (cut.kind === 'error') return true;
    }
    const grand = Object.assign({}, intent, { dims: [] });
    const opened = applyFormIntent(grand, gen, true);
    return finishIntent(withGen(grand, gen), gen, explanation,
      opened ? t('Metric form is open — specify a dimension and click "Calculate"') : null);
  }

  function withGen(intent, gen) {
    return Object.assign({}, intent || {}, { generator_id: gen.generator_id });
  }

  // Open the metrics panel on the matched KPI and pre-fill the period from
  // the intent's time filters. Returns false when that KPI has no button
  // (panel not rendered yet, KPI no longer ready). `quiet` leaves the status
  // line alone — the one-shot caller writes the whole sentence itself.
  function applyFormIntent(intent, gen, quiet) {
    const entry = metricEntries.find(e => e.kpi.key === intent.kpi
      && String(e.gen.generator_id) === String(gen.generator_id));
    if (!entry) return false;
    selectMetric(entry);
    (intent.filters || []).forEach(f => {
      if (!f || f.op !== '=' || !(f.dim in TIME_DIMS)) return;
      const input = metricForm.querySelector('[data-time="' + f.dim + '"]');
      if (input) input.value = String(f.value);
    });
    if (!quiet) {
      const label = entry.kpi.short || entry.kpi.name || entry.kpi.key;
      nlStatus.textContent = t('Found metric "{p1}" — select a dimension and click "Calculate"{p2}', {p1: label, p2: (entry.kpi.definition ? ' · ' + entry.kpi.definition : '')});
    }
    return true;
  }

  // ── tier 2: free SQL over the report's semantic views ────────────────
  // No KPI fits the question, but the report still knows what its own tables
  // and columns MEAN. The model gets that schema (names, types, the author's
  // Russian descriptions — never a value) and writes one SELECT.
  //
  // This SQL is model-written, so it is labelled as such and shown — and it
  // is still run, because an answer the operator can read and check beats a
  // query they have to press a second button for. The safety net is not the
  // click: it is that the SQL passed `is_select_only` + the engine's own
  // `guard_sql` on the server, and passes `guard_sql` again in the worker
  // before a read-only DuckDB ever sees it.
  const SEMANTIC_LABEL = t('SQL was written by the model from the report\'s semantics — check it');

  function bindingsStatusOf(gen) {
    return { tables: gen.tables || {}, views: gen.views || {} };
  }

  async function askSemanticSql(question, gen, cfg) {
    if (!modelAvailable(cfg)) return false;
    const body = { question: question, generator_id: gen.generator_id,
      bindings_status: bindingsStatusOf(gen) };
    let sql = null;
    if (cfg.browser && browserKey(cfg)) {
      sql = await semanticSqlViaBrowser(body, cfg);
      if (sql === 'reported') return true;
    }
    if (!sql && cfg.server) {
      const r = await fetch('/api/explore/semantic-sql', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let detail = '';
        try { detail = String((await r.json()).detail || ''); } catch (e) {}
        nlStatus.textContent = detail || (t('Error ({p1}).', {p1: r.status}));
        reportClientError('sql_server', nlStatus.textContent, { status: r.status, tier: 2 });
        return true;
      }
      sql = ((await r.json()).sql) || null;
    }
    if (!sql) return false;
    sqlBox.value = sql;
    nlStatus.textContent = t('{p1} · question: {p2}', {p1: SEMANTIC_LABEL, p2: question});
    // Model-written SQL: the operator is asked to check it, so the row that
    // holds it opens itself. This is the ONLY reason it ever opens.
    setSqlRowOpen(true);
    primeAnswer(question, null, null);
    await runQuery();
    return true;
  }

  // prepare -> the provider (plain SQL, no JSON mode) -> validate.
  // Returns the SQL, null (fall through to the server), or 'reported'.
  async function semanticSqlViaBrowser(body, cfg) {
    const rp = await fetch('/api/explore/semantic-sql/prepare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!rp.ok) { reportClientError('sql_prepare', 'HTTP ' + rp.status, { status: rp.status, tier: 2 }); return null; }
    const prep = await rp.json();
    const got = await callProvider(cfg, prep.prompt, null,
      t('You respond with strictly one SQL query. No explanations and no markdown.'), MAX_SQL_TOKENS);
    if (got.fallback) return null;
    if (got.reported) return 'reported';
    const rv = await fetch('/api/explore/semantic-sql/validate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: body.question, generator_id: body.generator_id, raw: got.raw }),
    });
    if (!rv.ok) {
      let detail = '';
      try { detail = String((await rv.json()).detail || ''); } catch (e) {}
      nlStatus.textContent = detail || (t('Error ({p1}).', {p1: rv.status}));
      reportClientError('sql_validate', nlStatus.textContent, { status: rv.status, tier: 2, sql: String(got.raw || '').slice(0, 400) });
      return 'reported';
    }
    return ((await rv.json()).sql) || null;
  }

  // The whole "Ask" decision tree for a folder with ready semantics.
  // Returns true when the question was answered (or the failure reported) and
  // false when the caller should fall through to tier 3 (bare nl2sql over the
  // raw catalogue), which now only happens when nothing semantic is indexed.
  // `fixedKpi` (P3b, "Clarify") — the reader has already said WHICH metric
  // this question is about, so the routing may choose the cut and the period
  // but not the metric. It is the same `fixed_kpi` the form-first path uses
  // for its second call; no new endpoint and no new server field.
  async function askIntent(question, gen, prevIntent, fixedKpi) {
    const cfg = await llmConfig();
    const out = await fetchIntent(question, gen, cfg, fixedKpi || null, prevIntent);
    // "Why …?": the server never lets such a question become a one-number
    // intent, so it never reaches finishIntent — it becomes a card.
    if (out.kind === 'why') {
      return whyCard(withGen(out.intent, gen), gen, out.why);
    }
    // A trend question with a metric resolved (by form or by model) becomes
    // the two-period card, whatever cut/limit the route proposed.
    if ((out.kind === 'intent' || out.kind === 'form') && out.intent && out.intent.kpi && isTrendQuestion(question)) {
      state.askVia = out.kind === 'intent' ? 'model' : 'form';
      return trendCard(withGen(out.intent, gen), gen, question, recentSpec(question));
    }
    if (out.kind === 'intent') {
      state.askVia = 'model';
      const flag = await verifyIntent(question, gen, out.intent, cfg);
      if (flag) {
        renderVerify(flag, () => { state.askVia = 'model';
          finishIntent(withGen(out.intent, gen), gen, out.explanation); },
          { intent: out.intent, gen: gen, question: question, explanation: out.explanation, via: 'model' });
        return true;
      }
      return finishIntent(withGen(out.intent, gen), gen, out.explanation);
    }
    if (out.kind === 'form') {
      state.askVia = 'form';
      return answerFormMatch(withGen(out.intent, gen), gen, question, cfg, out.explanation, prevIntent);
    }
    if (out.kind === 'error') return true;
    // A pinned metric with no model to choose the cut: the pinned metric over
    // the previous answer's own cut and period IS the answer, and it is
    // deterministic. Without this, "another metric" would be a button that
    // only works where a provider is configured.
    if (out.kind === 'nomodel' && fixedKpi) {
      state.askVia = 'form';
      return finishIntent(withGen({ kpi: String(fixedKpi),
        dims: (prevIntent && prevIntent.dims) || [],
        filters: (prevIntent && prevIntent.filters) || [] }, gen), gen, null);
    }
    // No model anywhere AND no form matched: nothing here can read the
    // question — but the metrics panel needs no model at all, so point at it
    // instead of leaving a dead end.
    if (out.kind === 'nomodel') {
      nlStatus.textContent = t('The model is not configured, and the question did not match any metric wording. Select a metric in the "Metrics" panel — it is calculated without a model.');
      reportClientError('no_model', nlStatus.textContent, { tier: 1 });
      return true;
    }
    // `none` = the model said "this is not one of the metrics" — the one case
    // tier 2 exists for.
    if (out.kind === 'none' && await askSemanticSql(question, gen, cfg)) return true;
    return false;
  }

  // Several generators ready at once: a chosen report card decides it
  // outright (the operator said which report they are asking about);
  // otherwise the one whose KPI names / question forms share the most words
  // with the question answers, ties keeping order.
  function pickGenerator(ready, question) {
    if (state.reportGen != null) {
      const pinned = ready.find(g => String(g.generator_id) === String(state.reportGen));
      if (pinned) return pinned;
    }
    const words = String(question || '').toLowerCase().split(/[^a-zа-яё0-9]+/).filter(w => w.length > 2);
    let best = ready[0], bestScore = -1;
    ready.forEach(g => {
      const text = (g.kpis || []).filter(k => k.ready)
        .map(k => [k.name, k.short].concat(k.question_forms || []).join(' ')).join(' ').toLowerCase();
      const score = words.reduce((s, w) => s + (text.indexOf(w) >= 0 ? 1 : 0), 0);
      if (score > bestScore) { best = g; bestScore = score; }
    });
    return best;
  }

  // The whole "Ask" pipeline as a function, so the chat view can ask a
  // follow-up without synthesising a click on a button that lives on the other
  // screen. `opts.prevIntent` is the currently OPEN answer's intent (phase 3):
  // the deterministic follow-ups are settled in explore-chat.js and never get
  // here, so what reaches this function is a follow-up only the model can read
  // — and the server is the one that decides what the previous intent may
  // contribute to the prompt.
  async function ask(question, opts) {
    question = String(question == null ? '' : question).trim();
    if (!question) return false;
    const prevIntent = (opts && opts.prevIntent) || null;
    const fixedKpi = (opts && opts.fixedKpi) || null;
    if (nlBox && (opts && opts.fill !== false)) nlBox.value = question;
    state.askQuestion = question;
    state.askCancelled = false;
    try { state.askAbort = (typeof AbortController === 'function') ? new AbortController() : null; }
    catch (e) { state.askAbort = null; }
    nlStatus.textContent = t('Asking…');
    if (nlBtn) nlBtn.disabled = true;
    try {
      // Tier 1 (metric) → tier 2 (free SQL over the report's semantics) →
      // tier 3 (bare nl2sql over the raw catalogue). Tier 3 is now reachable
      // only when NOTHING semantic is indexed: with a report's own
      // definitions loaded, guessing over raw 1C headers is a worse answer
      // than saying the two semantic tiers could not answer.
      const ready = readySemantics();
      if (ready.length) {
        const gen = pickGenerator(ready, question);
        state.askGen = gen ? gen.generator_id : null;
        // Phase 4.5, BEFORE any server call: "why is the number in the report 25.5?"
        // asked cold. Only this shape is intercepted — a why-question with no
        // number keeps the server-classifier route it has always had.
        // A pinned metric is an explicit answer to "which metric?", so the
        // value route (which exists to GUESS one) has nothing left to do.
        if (!prevIntent && !fixedKpi && await askValueRoute(question, gen)) return true;
        clearValueChips();
        if (await askIntent(question, gen, prevIntent, fixedKpi)) return true;
        nlStatus.textContent = t('Could not answer using the report\'s semantics. Rephrase the question or build the query in the builder.');
        return false;
      }
      const r = await fetch('/api/explore/nl2sql', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: question, table_ids: nlTableIds() }),
      });
      if (!r.ok) {
        // The server's `detail` distinguishes "no endpoint configured" from
        // "endpoint unreachable/timed out" — flattening both to one message
        // sent operators hunting a kill switch that was never off.
        let detail = '';
        try { detail = String((await r.json()).detail || ''); } catch (e) {}
        nlStatus.textContent = detail || (t('Error ({p1}).', {p1: r.status}));
        reportClientError('nl2sql', nlStatus.textContent, { status: r.status, tier: 3 });
        return false;
      }
      const data = await r.json();
      // Lands in the editor for review — the run button is a separate,
      // explicit click. This handler must never call runQuery().
      sqlBox.value = data.sql || '';
      // Tier 3 is model-written too and is NOT run — the operator has to read
      // it, so the row must be open when it lands.
      setSqlRowOpen(true);
      setAnswerTitle(question);
      nlStatus.textContent = data.explanation || t('SQL received — check it before running.');
      return true;
    } catch (e) {
      // An abort is not a failure to report: the operator asked for it, and
      // the chat's pending card already says "Cancelled".
      if (state.askCancelled || (e && e.name === 'AbortError')) {
        nlStatus.textContent = t('Cancelled.');
        // The flag belongs to THIS question and dies with it: the next one
        // may arrive through finishIntent (a follow-up, a clarify chip) and
        // never pass this function, so a sticky flag would silently cancel
        // a question nobody cancelled.
        state.askCancelled = false;
        emit('cancelled', { question: question });
        return false;
      }
      nlStatus.textContent = t('Could not reach the server.');
      reportClientError('ask_exception', (e && e.message) || String(e));
      return false;
    } finally {
      state.askAbort = null;
      if (nlBtn) nlBtn.disabled = false;
    }
  }

  // The question box grows with the text (1–5 lines) and sends on Enter;
  // Shift+Enter breaks the line — the same manners as the chat composer.
  if (nlBox) {
    const grow = () => { nlBox.style.height = 'auto'; nlBox.style.height = Math.min(120, nlBox.scrollHeight || 0) + 'px'; };
    nlBox.addEventListener('input', grow);
    nlBox.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); nlBtn && nlBtn.click(); }
    });
  }
  nlBtn && nlBtn.addEventListener('click', () => ask((nlBox.value || '').trim(),
    { fill: false, prevIntent: state.openAnswer && state.openAnswer.intent }));

  // ═════════════════════════════════════════════════════════════════
  // Page API (phase 3) — what `static/js/explore-chat.js` is allowed to use.
  // It loads AFTER this file, so the object exists by the time it runs. The
  // five documented members are `ask/runQuery/renderResult/state/on`; the
  // rest are the deterministic helpers a thread needs so the chat never
  // re-implements period arithmetic, KPI lookup or intent execution.
  // ═════════════════════════════════════════════════════════════════
  try {
    window.OP_EXPLORE = {
      ask: ask, runQuery: runQuery, renderResult: renderResult, state: state, on: on,
      reports: REPORTS, renderReports: renderReports, maybeServerAutoIndex: maybeServerAutoIndex, whenIndexed: whenIndexed,
      fmtValue: fmtValue,
      // executing an intent the chat built (the follow-up path) goes through
      // the SAME finishIntent every "Ask" answer goes through.
      finishIntent: finishIntent, whyCard: whyCard,
      shiftPeriod: shiftPeriod, readySemantics: readySemantics,
      pickGenerator: pickGenerator, withGen: withGen, kpiOf: kpiOf,
      metricChips: metricChips, periodLabel: periodLabel, intentStatus: intentStatus,
      renderMetricForm: renderMetricForm, metricOwnerLine: metricOwnerLine,
      setAnswerTitle: setAnswerTitle, setSqlStatus: setSqlStatus,
      setSqlBox: (v) => { if (sqlBox) sqlBox.value = v == null ? '' : v; },
      renderHow: renderHow, fmtNum: fmtNum, TIME_DIMS: TIME_DIMS,
      answerProvenance: answerProvenance,
      // "Cancel" on the chat's pending card. Not `killWorker`: that drops
      // the index with the query and costs a 30 s rebuild. This sets a flag
      // the two gates in runQuery read — before the engine is asked to run,
      // and before the answer is emitted.
      cancelAsk: () => {
      // Nothing is in flight — the answer has already landed (runQuery nulls
      // `askQuestion` right before it emits) or no question was ever started.
      // `askCancelled` is sticky, so a click that arrives now would poison the
      // NEXT question instead of stopping this one.
      if (!state.askAbort && !state.askQuestion) return;
      state.askCancelled = true;
      try { if (state.askAbort) state.askAbort.abort(); } catch (e) { /* already gone */ }
    },
      reportClientError: reportClientError,
      // Phase 9.2 — the partition cache is portable: a run can hand out the
      // Parquet it indexed (`cacheExport`) and any other user's browser can
      // take it in (`cacheImport`) without ever opening the workbooks. Both
      // are thin wrappers over the worker round trip with NO kill timer (0):
      // copying files is not a query, and a slow copy must never cost the
      // whole index the way `HARD_QUERY_TIMEOUT_MS` deliberately does.
      // The UI on top of them is phase 9.4.
      // The same file→catalog matching `runIndex` uses, so a caller that
      // publishes an index (a generator run, the Chrome harness) describes
      // its partitions exactly the way this page would have.
      matchFilesToCatalog: matchFilesToCatalog,
      cacheExport: (keys) => send('explore:cache_export', { keys: (keys || []).map(String) }, 0),
      cacheImport: (key, buf) => send('explore:cache_import', { key: String(key || ''), buf: buf }, 0),
      // Phase 4.5: the chat owns the thread (IndexedDB), so it lends its
      // records to the value lookup instead of explore.js learning the store.
      // `fn(gen) -> Promise<record[]>`; passing null unregisters.
      setHistoryProvider: (fn) => { state.historyProvider = typeof fn === 'function' ? fn : null; },
      userId: USER_ID,
    };
  } catch (e) { /* no window: the node require() path never gets here */ }

  // ── wiring ───────────────────────────────────────────────────────
  // "Stop": the only way to stop a synchronous engine call is to drop
  // the worker — the index goes with it, and the status says so.
  // "Load the report's index" — the primary way in: the focused report's
  // published index. Shown only when such a report has one.
  const loadIndexBtn = document.getElementById('ex-load-index-btn');
  // Folder tools are a SERVICE mode: `?folder=1`, or a catalogue with no
  // report cards at all (nothing else could feed it). Everyone else loads
  // the report's index, which the run built over ALL its folders.
  const folderTools = document.getElementById('ex-folder-tools');
  if (folderTools) folderTools.hidden = !FOLDER_MODE;
  // "Check folder" is a folder tool too.
  const checkRow = document.getElementById('ex-row-check');
  if (checkRow && !FOLDER_MODE) checkRow.hidden = true;
  function syncLoadIndexBtn() {
    if (!loadIndexBtn) return;
    const rep = focusedReport();
    const man = rep && state.serverManifests[String(rep.generator_id)];
    loadIndexBtn.hidden = !(man && !(state.indexed && state.source === 'server'));
    const hint = document.getElementById('ex-sources-hint');
    if (hint) {
      hint.textContent = man ? t('The report\'s index is built when calculating across all the report\'s folders and is stored on the server.')
        : rep ? t('This report\'s index has not been published yet — recalculate the report (Run) to build the index across all folders.')
              : t('Select a report — its index will load from the server.');
    }
  }
  loadIndexBtn && loadIndexBtn.addEventListener('click', () => { const rep = focusedReport(); if (rep) loadServerIndex(rep); });
  abortBtn && abortBtn.addEventListener('click', () => {
    killWorker(t('Interrupted — the index has been reset, load it again.'));
    abortBtn.hidden = true;
  });
  connectBtn && connectBtn.addEventListener('click', connectFolder);
  reconnectBtn && reconnectBtn.addEventListener('click', reconnectFolder);
  indexBtn && indexBtn.addEventListener('click', runIndex);

  populateTableSelect();
  renderReports();
  loadSaved();
  // Early, so the BYOK key field is on screen before the first question
  // rather than appearing after it failed for want of a key.
  llmConfig();
  if (typeof indexedDB !== 'undefined') initFromStoredHandle();
  else maybeServerAutoIndex();
  // Phase 9.4: which reports have an index published on the server. Deferred
  // past first paint on purpose — the cards render without it, and `/` (the
  // hot path) never asks at all: this runs only on /explore.
  setTimeout(probeServerIndexes, 0);

  // ═════════════════════════════════════════════════════════════════
  // "Check folder" (H2) — deterministic data-quality findings over the
  // already-indexed folder, no generator run, no model. Self-contained on
  // purpose: everything this feature needs (button, panel, worker call,
  // rendering) lives in this one block so it never touches the askIntent
  // region above. Talks to the worker's 'explore:check' message
  // (gen-worker.js), answered by static/py/_folder_check.py.
  // ═════════════════════════════════════════════════════════════════
  (function folderCheckFeature() {
    const checkBtn = document.getElementById('ex-check-btn');
    const checkPanel = document.getElementById('ex-check-panel');
    const checkTitle = document.getElementById('ex-check-title');
    const checkList = document.getElementById('ex-check-list');
    const checkHide = document.getElementById('ex-check-hide');
    if (!checkBtn || !checkPanel || !checkList) return;

    const SEV_LABEL = { warn: t('warning'), info: t('info'), error: t('error') };

    // The button only makes sense once something is indexed; state.indexed
    // flips inside applyIndexed()/killWorker() elsewhere in this file, so a
    // light poll is the least invasive way to track it without editing
    // those functions from this isolated block.
    // FOLDER-only (phase 9.4): the findings are about the files of a share —
    // an index pulled from the server has no folder to check.
    const enabled = () => !!(state.indexed && state.dirHandle);
    const pollTimer = setInterval(() => { checkBtn.disabled = !enabled(); }, 300);
    // Node (the DOM test harness) returns a Timeout object here and keeps the
    // event loop — and so the process — alive for as long as this runs;
    // `unref()` (absent in real browsers, where setInterval returns a plain
    // number with no such method) tells Node this timer alone must not hold
    // the process open. Purely a test-harness nicety: in a real tab the timer
    // is meant to outlive everything until the page itself unloads.
    if (pollTimer && typeof pollTimer.unref === 'function') pollTimer.unref();
    checkBtn.disabled = !enabled();

    function renderFindings(findings, note) {
      checkList.textContent = '';
      // An engine that did not RUN the checks must not read as "clean": v2
      // answers `{findings: [], note}` and the note is what the operator sees.
      if (!findings.length && note) {
        checkTitle.textContent = note;
        checkPanel.hidden = false;
        return;
      }
      if (!findings.length) {
        checkTitle.textContent = t('Folder check: no issues found.');
        checkPanel.hidden = false;
        return;
      }
      checkTitle.textContent = t('Folder check: {p1}', {p1: nOf(findings.length, [t('issue'), t('findings'), t('issues')])});
      findings.forEach((f) => {
        const li = el('li', { class: 'ex-check-item sev-' + (f.severity || 'warn') }, [
          el('span', { class: 'ex-check-sev', text: SEV_LABEL[f.severity] || f.severity || '' }),
          el('span', { class: 'ex-check-msg', text: f.message || '' }),
        ]);
        if (f.sample && f.sample.length) {
          li.appendChild(el('span', { class: 'ex-check-sample', text: '(' + f.sample.join(', ') + ')' }));
        }
        checkList.appendChild(li);
      });
      checkPanel.hidden = false;
    }

    // The panel now lives inside a collapsed row; the button that fills it
    // sits in the folder cell, so it must open that row itself — otherwise
    // the findings render into something nobody can see.
    const checkRow = document.getElementById('ex-row-check');

    checkBtn.addEventListener('click', async () => {
      if (!state.indexed) { setSummary(t('First connect a folder with files and update the index.')); return; }
      if (checkRow) checkRow.open = true;
      checkBtn.disabled = true;
      checkTitle.textContent = t('Checking the folder…');
      checkList.textContent = '';
      checkPanel.hidden = false;
      try {
        const res = await send('explore:check', { checks: [] }, 60000);
        if (res && res.error) {
          checkTitle.textContent = t('Folder check: {p1}', {p1: res.error});
          checkList.textContent = '';
          return;
        }
        renderFindings((res && res.findings) || [], res && res.note);
      } finally {
        checkBtn.disabled = !enabled();
      }
    });

    checkHide && checkHide.addEventListener('click', () => { checkPanel.hidden = true; });
  })();
})();
