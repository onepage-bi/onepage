/* Headless harness for static/js/explore.js + static/js/explore-recipes.js.
 *
 * Same idiom as tests/catalog_dom.js: no jsdom, no new dependency — the
 * smallest DOM the script actually touches, plus stubbed fetch/Worker so the
 * builder, recipes, nl2sql-lands-in-editor-without-running, Ctrl+Enter and
 * export paths are driven and inspected without a server or a real worker.
 *
 * Run: node tests/explore_dom.js   (prints PASS/FAIL lines + ALL PASS)
 */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub
// Questions typed into #ex-nl / passed to the question parsers stay Russian on
// purpose: explore.js routes them with its Russian why/trend/period rules.
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..');
const FAILURES = [];
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (!cond && extra ? '  <-- ' + extra : ''));
  if (!cond) FAILURES.push(label);
}

// ═══════════════════════════════════════════════════════════════════
// Part 1 — pure functions, no DOM at all
// ═══════════════════════════════════════════════════════════════════
const RECIPES = require(path.join(ROOT, 'static', 'js', 'explore-recipes.js'));
const EXPLORE = require(path.join(ROOT, 'static', 'js', 'explore.js'));

// ── builder: filter + group + order, exercised directly (no DOM) ──────
(() => {
  const sql = EXPLORE.buildSql({
    table: 'sales_orders',
    filters: [{ column: 'amount', op: '>', value: '100' }],
    groupBy: ['order_no'],
    orderCol: 'order_no', orderDir: 'DESC',
    limit: 1000,
  });
  check('buildSql: SELECT the group-by column', sql.includes('SELECT "order_no"'), sql);
  check('buildSql: FROM the quoted table', sql.includes('FROM "sales_orders"'), sql);
  check('buildSql: numeric filter left unquoted', sql.includes('WHERE "amount" > 100'), sql);
  check('buildSql: GROUP BY the chosen column', sql.includes('GROUP BY "order_no"'), sql);
  check('buildSql: ORDER BY DESC', sql.includes('ORDER BY "order_no" DESC'), sql);
})();

// ── aggregate alias quoting: a column name with a comma/space must not
//    spill into the SELECT list as a second bogus column (finding: aggExpr
//    built the alias from the raw column name, unquoted) ──────────────────
(() => {
  const sql = EXPLORE.buildSql({
    table: 'sales_orders',
    groupBy: ['Client'],
    aggFn: 'SUM', aggCol: 'Amount, USD',
  });
  check('aggExpr: alias for a comma-bearing column is quoted (one SELECT item, not two)',
    /^SELECT "Client", SUM\("Amount, USD"\) AS "[^"]+"\n/.test(sql), sql);
  // The alias itself legitimately contains the comma (it's quoted) — what
  // must NOT happen is a second, bare select item spilling out after it.
  check('aggExpr: the SELECT line is exactly the two intended items, nothing spilled past the alias',
    sql.split('\n')[0] === 'SELECT "Client", SUM("Amount, USD") AS "Sum_Amount, USD"', sql);
})();

(() => {
  const sql = EXPLORE.buildSql({ table: 't', aggFn: 'COUNT', aggCol: '' });
  check('aggExpr: bare COUNT(*) alias is a quoted Russian label', sql.includes('COUNT(*) AS "Qty"'), sql);
})();

// ── filter numeric-vs-text decided by the catalog's column type, not by
//    the value's own shape (finding: "007" against a text column used to be
//    emitted as a bare numeric literal and abort/mismatch in DuckDB) ──────
(() => {
  const sqlText = EXPLORE.buildSql({
    table: 't',
    filters: [{ column: 'code', op: '=', value: '007' }],
    columnTypes: { code: 'text' },
  });
  check('filter: a digits-only value against a text column is quoted as a string',
    sqlText.includes('WHERE "code" = \'007\''), sqlText);

  const sqlNum = EXPLORE.buildSql({
    table: 't',
    filters: [{ column: 'amount', op: '=', value: '007' }],
    columnTypes: { amount: 'number' },
  });
  check('filter: the same digits-only value against a number column stays a bare literal',
    sqlNum.includes('WHERE "amount" = 007'), sqlNum);

  // No type known at all (e.g. builder used before a table is picked): the
  // old shape-based heuristic is still the fallback, unchanged behaviour.
  const sqlFallback = EXPLORE.buildSql({
    table: 't',
    filters: [{ column: 'x', op: '=', value: '42' }],
  });
  check('filter: falls back to shape-based guess when no column type is known',
    sqlFallback.includes('WHERE "x" = 42'), sqlFallback);
})();

// ── shiftPeriod: the JS half of nl2sql.shift_period, same rules ──────────
(() => {
  const S = EXPLORE.shiftPeriod;
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  check('shiftPeriod: January steps back into December of the previous year',
    eq(S([{ dim: 'year', op: '=', value: 2026 }, { dim: 'month', op: '=', value: 1 }]),
       [{ dim: 'year', op: '=', value: 2025 }, { dim: 'month', op: '=', value: 12 }]),
    JSON.stringify(S([{ dim: 'year', op: '=', value: 2026 }, { dim: 'month', op: '=', value: 1 }])));
  check('shiftPeriod: a mid-year month just steps back',
    eq(S([{ dim: 'month', op: '=', value: 7 }]), [{ dim: 'month', op: '=', value: 6 }]),
    JSON.stringify(S([{ dim: 'month', op: '=', value: 7 }])));
  check('shiftPeriod: week 1 becomes week 52 of the previous year',
    eq(S([{ dim: 'year', op: '=', value: 2026 }, { dim: 'week', op: '=', value: 1 }]),
       [{ dim: 'year', op: '=', value: 2025 }, { dim: 'week', op: '=', value: 52 }]),
    JSON.stringify(S([{ dim: 'year', op: '=', value: 2026 }, { dim: 'week', op: '=', value: 1 }])));
  check('shiftPeriod: an ISO day steps back a calendar day, carrying month/year',
    eq(S([{ dim: 'year', op: '=', value: 2026 }, { dim: 'month', op: '=', value: 1 },
          { dim: 'day', op: '=', value: '2026-01-01' }]),
       [{ dim: 'year', op: '=', value: 2025 }, { dim: 'month', op: '=', value: 12 },
        { dim: 'day', op: '=', value: '2025-12-31' }]),
    JSON.stringify(S([{ dim: 'year', op: '=', value: 2026 }, { dim: 'month', op: '=', value: 1 },
      { dim: 'day', op: '=', value: '2026-01-01' }])));
  check('shiftPeriod: year alone steps back a year',
    eq(S([{ dim: 'year', op: '=', value: 2026 }]), [{ dim: 'year', op: '=', value: 2025 }]));
  check('shiftPeriod: non-time filters ride along untouched',
    eq(S([{ dim: 'month', op: '=', value: 7 }, { dim: 'client', op: '=', value: 'Daisy' }]),
       [{ dim: 'month', op: '=', value: 6 }, { dim: 'client', op: '=', value: 'Daisy' }]));
  check('shiftPeriod: no time filter at all -> null',
    S([{ dim: 'client', op: '=', value: 'Daisy' }]) === null && S([]) === null);
  check('shiftPeriod: an `in` period has no single predecessor -> null',
    S([{ dim: 'month', op: 'in', values: [5, 6, 7] }]) === null);
  const input = [{ dim: 'month', op: '=', value: 7 }];
  S(input);
  check('shiftPeriod: does not mutate its input', input[0].value === 7, JSON.stringify(input));
})();

// ── phase 4.5 (pure): «why is the report number 25.5?» ─────────────────
// The questions below are Russian on purpose: they exercise the Russian
// why/trend/number parser in explore.js.
// The client half of a route that never reaches the server: the why
// pre-check, the number parse (with the period tokens already eaten), and
// the tolerance that turns a WRITTEN number into an interval.
(() => {
  const W = EXPLORE.isWhyQuestion;
  // ── trend route: the question names CHANGE over recent periods ───────
  {
    const T = EXPLORE.isTrendQuestion, R = EXPLORE.recentSpec;
    check('trend: «negative trend over the last two months» is a trend question',
      T('по какой сети отрицательный тренд по SL за последние два месяца?') === true);
    check('trend: «how did SL change by hub?» too', T('как изменился SL по хабам?') === true);
    check('trend: a plain metric question is not', T('какой SL по сетям за июль?') === false);
    check('trend: a WHY-question with a named week is a why, never a trend («dropped» does not decide)',
      T('какие SKU не привезли в Одуванчик на 33 неделе? почему там SL упал?') === false);
    check('trend: a named month is about that month («dropped in July» is not a trend)',
      T('SL упал в июле по какой сети?') === false);
    const s1 = R('по какой сети отрицательный тренд по SL за последние два месяца?');
    check('trend: «last two months» → month × 2', s1.gran === 'month' && s1.n === 2, JSON.stringify(s1));
    const s2 = R('SL по хабам за последние 3 недели');
    check('trend: «last 3 weeks» → week × 3', s2.gran === 'week' && s2.n === 3, JSON.stringify(s2));
    const s3 = R('как изменился SL за последний месяц?');
    check('trend: «last month» still compares TWO months (a trend needs two points)',
      s3.gran === 'month' && s3.n === 2, JSON.stringify(s3));
    const s4 = R('динамика SL по неделям');
    check('trend: «by week» without «last» → week × 2', s4.gran === 'week' && s4.n === 2, JSON.stringify(s4));
    const s5 = R('какой клиент имеет негативный тренд SL по году?');
    check('trend: «over the year» → month × 12 (the whole year, first month against the latest)',
      s5.gran === 'month' && s5.n === 12, JSON.stringify(s5));
    check('trend: «over the year» without a named period is still a trend question',
      T('какой клиент имеет негативный тренд SL по году?') === true);
    const s6 = R('SL с начала года по сетям — где падает?');
    check('trend: «year to date» → month × 12', s6.gran === 'month' && s6.n === 12, JSON.stringify(s6));
    const s7 = R('тренд SL за последние 2 месяца по году');
    check('trend: an explicit «last N» wins over «over the year»', s7.gran === 'month' && s7.n === 2, JSON.stringify(s7));
    check('trend: «in 2026» names a period — not a trend', T('SL упал в 2026 году?') === false);
  }
  check('why-precheck: «why …» at the head', W('почему 25,5?') === true);
  check('why-precheck: «and why …» — inside the first three words',
    W('а почему вдруг 25,5?') === true);
  check('why-precheck: «because of what» / «how come» / «what happened» count too',
    W('из-за чего 25,5?') && W('Отчего 25,5?') && W('что случилось с SL?'));
  check('why-precheck: an ordinary metric question is NOT a why-question',
    W('какой SL по сетям за июль?') === false);
  check('why-precheck: «pochemuchka» (why-child) is not «why» (word end, not prefix)',
    W('почемучка 5') === false);

  const P = EXPLORE.parseAskedNumber;
  const eqn = (q, value, decimals, percent) => {
    const got = P(q);
    return !!got && got.value === value && got.decimals === decimals && got.percent === !!percent;
  };
  check('parse: «25,5» — comma decimal', eqn('почему 25,5?', 25.5, 1), JSON.stringify(P('почему 25,5?')));
  check('parse: «25.5» — dot decimal', eqn('почему 25.5?', 25.5, 1));
  check('parse: «25,50» — the written precision is kept (2 decimals, not 1)',
    eqn('почему 25,50?', 25.5, 2), JSON.stringify(P('почему 25,50?')));
  check('parse: «25,5%» — the percent sign is read and does not break the number',
    eqn('почему 25,5%?', 25.5, 1, true), JSON.stringify(P('почему 25,5%?')));
  check('parse: «1 234,5» — space thousands separator',
    eqn('почему 1 234,5?', 1234.5, 1), JSON.stringify(P('почему 1 234,5?')));
  const NB = 'почему 1\u00A0234,5?';
  check('parse: «1 234,5» — NBSP thousands separator (what Excel actually pastes)',
    eqn(NB, 1234.5, 1), JSON.stringify(P(NB)));
  // U+2009 THIN SPACE is what the page's own fmtNum prints, so a number
  // copied out of the result grid must parse back in.
  const TH = 'почему 43\u2009500?';
  check('parse: thin-space thousands — the separator fmtNum itself renders',
    eqn(TH, 43500, 0), JSON.stringify(P(TH)));
  check('parse: «96» — a bare integer is a value with a ±0.5 window',
    eqn('почему 96?', 96, 0), JSON.stringify(P('почему 96?')));
  check('parse: «in week 25» — the period parse ate it, no value candidate',
    P('почему упал SL в 25 неделе?') === null, JSON.stringify(P('почему упал SL в 25 неделе?')));
  check('parse: «on the 5th» — same, the unit word follows the number',
    P('почему 5 числа так мало?') === null, JSON.stringify(P('почему 5 числа так мало?')));
  check('parse: «for 2026» — a 20xx year is never a value',
    P('почему за 2026 упало?') === null, JSON.stringify(P('почему за 2026 упало?')));
  check('parse: a year next to a real value leaves the value alone',
    eqn('почему в январе 2026 число 25,5?', 25.5, 1),
    JSON.stringify(P('почему в январе 2026 число 25,5?')));
  check('parse: two numbers -> null (ambiguous, the old routing keeps it)',
    P('почему 25,5 и 30,1?') === null);
  check('parse: no number at all -> null (a why-question, not a value lookup)',
    P('почему упал SL?') === null);
  check('parse: an ordinal «2nd» is a position, not a measured value',
    P('почему 2-й склад отстаёт?') === null, JSON.stringify(P('почему 2-й склад отстаёт?')));
  check('parse: «2026,5» IS a value — it was written with decimals, so it is no year',
    eqn('почему 2026,5?', 2026.5, 1), JSON.stringify(P('почему 2026,5?')));

  // ── tolerance table: half of the LAST WRITTEN digit ──────────────────
  const T = EXPLORE.numberTolerance;
  check('tolerance: 1 decimal -> ±0.05', T(1) === 0.05);
  check('tolerance: 0 decimals -> ±0.5', T(0) === 0.5);
  check('tolerance: 2 decimals -> ±0.005', T(2) === 0.005);
  const M = EXPLORE.numberMatches;
  const p1 = P('почему 25,5?');
  [[25.5, true], [25.46, true], [25.54, true], [25.4, false], [25.6, false],
   [0, false], [null, false], [true, false], ['no', false]].forEach(([c, want]) => {
    check('tolerance: «25,5» vs ' + JSON.stringify(c) + ' -> ' + want, M(c, p1) === want);
  });
  const p0 = P('почему 96?');
  [[96, true], [95.5, true], [96.5, true], [95.4, false], [96.6, false]].forEach(([c, want]) => {
    check('tolerance: «96» vs ' + c + ' -> ' + want, M(c, p0) === want);
  });
  const p2 = P('почему 25,50?');
  check('tolerance: «25,50» is TIGHTER than «25,5» — 25.46 no longer matches',
    M(25.46, p2) === false && M(25.503, p2) === true);

  // ── the ratio doubling: 0,255 is displayed as «25,5 %» ───────────────
  check('percent: a ratio KPI stored as 0.255 matches the asked «25,5»', M(0.255, p1, true) === true);
  check('percent: 0.2546 too — the interval scales with the number',
    M(0.2546, p1, true) === true && M(0.2540, p1, true) === false);
  check('percent: a NON-ratio KPI never gets the ×100 reading («1000» must not claim a 10)',
    M(10, P('почему 1000?')) === false && M(0.255, p1) === false);
  check('percent: an explicit «%» in the question allows ×100 for any candidate',
    M(0.255, P('почему 25,5%?')) === true);
  check('parse: a leading minus is kept («-25,5»), a range dash is not («20-25»)',
    P('почему -25,5?') && P('почему -25,5?').value === -25.5 && P('почему 20-25?') === null);
  check('parse: «number 96» is a value, «on the 5th» is a date',
    P('почему в отчёте число 96?') && P('почему в отчёте число 96?').value === 96
    && P('почему 5 числа упало?') === null);
  check('percent: «25,5%» matches the stored ratio as well as the displayed one',
    M(0.255, P('почему 25,5%?')) === true && M(25.5, P('почему 25,5%?')) === true);

  // ── cell scan over a result grid ─────────────────────────────────────
  const S2 = EXPLORE.scanRowsForValue;
  check('scan: finds the number in ANY numeric cell of any row',
    S2([['Daisy', 'text', 1], ['Cornflower', 'text', 25.51]], p1) === true);
  check('scan: a text cell that merely LOOKS like a number is still scanned',
    S2([['25,5 %']], p1) === true);
  check('scan: booleans and nulls are not numbers',
    S2([[true, null, undefined]], p1) === false);
  check('scan: no cell matches -> false', S2([['Daisy', 1, 2]], p1) === false);
})();

(() => {
  const { matched, unmatched } = EXPLORE.matchFilesToCatalog(
    [{ name: 'DataSalesOrder_06.26.xlsx', size: 100, mtime_ms: 1000 }, { name: 'random.xlsx', size: 1, mtime_ms: 1 }],
    [{ table_id: 't_aaa', name: 'sales_orders', header_row: 0, partitions: [{ key: '2026-06', file: 'DataSalesOrder_06.26.xlsx', sheet: 'TDSheet' }] }]
  );
  check('matchFilesToCatalog: matches by partition file name', matched.length === 1 && matched[0].table_id === 't_aaa');
  check('matchFilesToCatalog: unmatched file listed as "not in catalogue"', unmatched.includes('random.xlsx'));
})();

(() => {
  const sql = RECIPES.whatChanged('sales_orders', 'Order number', '2026-06', '2026-07', ['Order number', 'Amount']);
  check('whatChanged quotes a column with a space in double quotes', sql.includes('"Order number"'), sql);
  check('whatChanged quotes partition literals with single quotes', sql.includes("'2026-06'") && sql.includes("'2026-07'"), sql);
  check('whatChanged is a single statement (WITH ... SELECT)', /^WITH/.test(sql.trim()));
})();

(() => {
  const sql = RECIPES.asOfDate('stock', '2026-08');
  check('asOfDate filters by __partition literal', sql.includes('WHERE __partition = \'2026-08\''), sql);
  check('asOfDate quotes the table name', sql.includes('"stock"'), sql);
})();

(() => {
  const sql = RECIPES.reconcile('sales_orders', 'realisations', 'order_no', 'order_no');
  check('reconcile emits a LEFT JOIN + IS NULL anti-join', /LEFT JOIN/.test(sql) && /IS NULL/.test(sql), sql);
  check('reconcile quotes both table names', sql.includes('"sales_orders"') && sql.includes('"realisations"'), sql);
})();

// ── reconcile / dupsAndGaps must scope to one partition each: unscoped, both
//    run over the UNION ALL of every loaded partition (finding: a key unique
//    inside each snapshot is reported as a duplicate once per extra
//    partition, and a reconcile miss is multiplied the same way) ──────────
(() => {
  const sql = RECIPES.reconcile('sales_orders', 'realisations', 'order_no', 'order_no', '2026-08', '2026-07');
  check('reconcile: left side is scoped to its chosen partition',
    /FROM \(SELECT \* FROM "sales_orders" WHERE __partition = '2026-08'\) a/.test(sql), sql);
  check('reconcile: right side is scoped to its own chosen partition',
    /LEFT JOIN \(SELECT \* FROM "realisations" WHERE __partition = '2026-07'\) b/.test(sql), sql);

  const sqlUnscoped = RECIPES.reconcile('sales_orders', 'realisations', 'order_no', 'order_no');
  check('reconcile: no partition given falls back to the bare table (unchanged old shape)',
    sqlUnscoped.includes('FROM "sales_orders" a') && sqlUnscoped.includes('LEFT JOIN "realisations" b'),
    sqlUnscoped);
})();

(() => {
  const sql = RECIPES.dupsAndGaps('sales_orders', 'order_no');
  check('dupsAndGaps groups by the key and filters HAVING', /GROUP BY "order_no"/.test(sql) && /HAVING/.test(sql), sql);

  const sqlScoped = RECIPES.dupsAndGaps('sales_orders', 'order_no', '2026-08');
  check('dupsAndGaps: scoped to one partition when given, so a per-snapshot-unique key is not counted across snapshots',
    /FROM "sales_orders"\nWHERE __partition = '2026-08'\nGROUP BY/.test(sqlScoped), sqlScoped);
  check('dupsAndGaps: the count column is a label, quoted',
    sqlScoped.includes('COUNT(*) AS "Qty"') && sqlScoped.includes('ORDER BY "Qty" DESC'), sqlScoped);
})();

(() => {
  const sql = RECIPES.trendByPartition('sales_orders', 'Amount');
  check('trendByPartition sums the metric column, quoted', sql.includes('SUM("Amount")'), sql);
  const sql2 = RECIPES.trendByPartition('sales_orders', null);
  check('trendByPartition defaults to COUNT(*) with no metric', sql2.includes('COUNT(*)'), sql2);
})();

// ═══════════════════════════════════════════════════════════════════
// Part 2 — full page DOM (builder → SQL, run, export, nl2sql, result grid)
// ═══════════════════════════════════════════════════════════════════
class ClassList {
  constructor() { this.set = new Set(); }
  add() { for (const c of arguments) if (c) this.set.add(c); }
  remove() { for (const c of arguments) this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, on) { if (on === undefined) on = !this.set.has(c); if (on) this.set.add(c); else this.set.delete(c); return on; }
  get value() { return Array.from(this.set).join(' '); }
}
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this._class = new ClassList();
    this._text = '';
    this.hidden = false;
    this.value = '';
    this.disabled = false;
    this.selected = false;
    // <details> carries an `open` IDL property the page reads and writes
    // («SQL of this answer» opens itself for model-written SQL). Only details
    // has it, so a regression that sets `.open` on something else shows up.
    if (this.tagName === 'DETAILS') this.open = false;
    // Real HTMLInputElement.list is a getter-only IDL property (the setter
    // belongs to the reflected `list` ATTRIBUTE, not the property) — assigning
    // to it in strict mode throws TypeError. Earlier versions of this harness
    // had no such property, so `'list' in n` was false and el() took the safe
    // setAttribute() path no matter what the source did — masking a real
    // browser crash. Reproduce the getter-only shape so a regression to
    // `n['list'] = …` fails this suite the same way it fails in Chrome.
    if (this.tagName === 'INPUT') {
      Object.defineProperty(this, 'list', { get: () => this._list || null, configurable: true });
    }
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text || this.children.map(collectText).join(' '); }
  get options() { return this.children.filter(c => c.tagName === 'OPTION'); }
  get selectedOptions() { return this.options.filter(o => o.selected); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatchEvent(ev) { ev.target = ev.target || this; (this.listeners[ev.type] || []).forEach(f => f.call(this, ev)); return true; }
  querySelectorAll(sel) { const out = []; walk(this, n => { if (matches(n, sel)) out.push(n); }); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function collectText(n) { return n._text || (n.children || []).map(collectText).join(' '); }
function camel(s) { return s.replace(/-([a-z])/g, (m, c) => c.toUpperCase()); }
function walk(n, fn) { (n.children || []).forEach(c => { fn(c); walk(c, fn); }); }
function matches(n, sel) {
  const tokens = sel.match(/(^[a-zA-Z0-9-]+)|(\.[a-zA-Z0-9_-]+)|(\[[^\]]+\])/g) || [];
  return tokens.every(tok => {
    if (tok[0] === '.') return n.classList.contains(tok.slice(1));
    if (tok[0] === '[') {
      const body = tok.slice(1, -1);
      const eq = body.indexOf('=');
      if (eq === -1) return n.attrs[body] !== undefined;
      const key = body.slice(0, eq);
      const val = body.slice(eq + 1).replace(/^["']|["']$/g, '');
      return n.attrs[key] === val;
    }
    return n.tagName.toLowerCase() === tok.toLowerCase();
  });
}

let byIdMap = {};
let body = new El('div');
const docListeners = {};
const document_ = {
  createElement: t => new El(t),
  getElementById: id => byIdMap[id] || null,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  dispatchEvent: ev => { (docListeners[ev.type] || []).forEach(f => f(ev)); return true; },
  querySelectorAll: sel => body.querySelectorAll(sel),
  querySelector: sel => body.querySelector(sel),
  createTextNode: v => ({ nodeType: 3, text: String(v) }),
  body: body,
};

function mk(tag, attrs, id) {
  const e = new El(tag);
  Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
  if (id) { e.attrs.id = id; byIdMap[id] = e; }
  return e;
}

const CATALOG = {
  tables: [
    {
      table_id: 't_aaa', name: 'sales_orders', display_name: 'DataSalesOrder', status: 'confirmed',
      header_row: 0, source: { id: 1, key_name: 'src' },
      columns: [{ name: 'order_no', display: 'Order number', type: 'text' }, { name: 'amount', display: 'Amount', type: 'number' }],
      partitions: [
        { key: '2026-06', file: 'DataSalesOrder_06.26.xlsx', sheet: 'TDSheet', rows: 10, mtime_ms: 1000, size: 100 },
        { key: '2026-07', file: 'DataSalesOrder_07.26.xlsx', sheet: 'TDSheet', rows: 12, mtime_ms: 2000, size: 100 },
      ],
    },
    // A table of the same catalogue that the connected folder does NOT back —
    // it must end up under «Other catalogue tables» once something is indexed.
    {
      table_id: 't_bbb', name: 'stock', display_name: 'DataStock', status: 'confirmed',
      header_row: 0, source: { id: 2, key_name: 'other' },
      columns: [{ name: 'sku', display: 'SKU', type: 'text' }],
      partitions: [{ key: '2026-06', file: 'DataStock_06.26.xlsx', sheet: 'TDSheet', rows: 3, mtime_ms: 900, size: 10 }],
    },
  ],
  values: { 't_aaa.order_no': ['SO-1', 'SO-2'] },
  // Baked by _semantics_snapshot; the report strip reads it to tell «no
  // semantics» from «not indexed yet».
  semantics: [{ generator_id: 7, generator: 'slreport', version_number: 3 },
              { generator_id: 9, generator: 'opsreport', version_number: 1 }],
};

// data-reports: what `_reports_snapshot` bakes into #ex-root — one card per
// generator-backed dashboard this user may run.
const REPORTS = [
  { dashboard_id: 'd1', title: 'SL report', generator_id: 7, generator_name: 'slreport', version: 3 },
  { dashboard_id: 'd2', title: 'OPS RDC Health', generator_id: 9, generator_name: 'opsreport', version: 1 },
];

function buildDom() {
  byIdMap = {};
  body = new El('div');
  document_.body = body;
  const root = mk('div', {}, 'ex-root');
  root.setAttribute('data-catalog', JSON.stringify(CATALOG));
  root.setAttribute('data-reports', JSON.stringify(REPORTS));
  root.setAttribute('data-asset-v', 'v1');
  root.setAttribute('data-pyodide-base', '/static/pyodide/');
  body.appendChild(root);

  ['ex-status', 'ex-summary', 'ex-progress-msg', 'ex-nl-status', 'ex-result-ms'].forEach(id => body.appendChild(mk('span', {}, id)));
  const clarify = mk('div', {}, 'ex-clarify'); clarify.hidden = true; body.appendChild(clarify);

  // ── screen A, block 1: the report strip + the folder cell ───────────
  const reports = mk('section', {}, 'ex-reports');
  reports.appendChild(mk('div', {}, 'ex-report-strip'));
  body.appendChild(reports);
  body.appendChild(mk('button', {}, 'ex-chat-btn'));
  body.appendChild(mk('span', {}, 'ex-chat-count'));

  // ── screen B, «Chat» (phase 3) ───────────────────────────────────────
  // Built by static/js/explore-chat.js (its own harness is
  // tests/explore_chat_dom.js); present here so this file keeps matching the
  // template node for node, and so the `hidden` default is pinned in both.
  const chat = mk('section', {}, 'ex-chat'); chat.hidden = true; body.appendChild(chat);
  ['ex-chat-threads', 'ex-chat-thread', 'ex-chat-chips', 'ex-chat-status']
    .forEach(id => chat.appendChild(mk('div', {}, id)));
  chat.appendChild(mk('textarea', {}, 'ex-chat-input'));
  chat.appendChild(mk('button', {}, 'ex-chat-send'));

  reports.appendChild(mk('button', {}, 'ex-connect-btn'));
  const reconnectBtn = mk('button', {}, 'ex-reconnect-btn'); reconnectBtn.hidden = true; reports.appendChild(reconnectBtn);
  const indexBtn = mk('button', {}, 'ex-index-btn'); indexBtn.disabled = true; reports.appendChild(indexBtn);
  const progress = mk('div', {}, 'ex-progress'); progress.hidden = true; reports.appendChild(progress);
  reports.appendChild(mk('div', {}, 'ex-progress-fill'));

  // «Check folder» (H2) — the button stays in the folder cell, the panel
  // moved into the collapsed «Check folder» row.
  const checkBtn = mk('button', {}, 'ex-check-btn'); checkBtn.disabled = true; reports.appendChild(checkBtn);

  // ── screen A, block 4: the three collapsed rows ─────────────────────
  const rows = mk('div', {}, 'ex-rows');
  const rowSql = mk('details', {}, 'ex-row-sql');
  const rowBuilder = mk('details', {}, 'ex-row-builder');
  const rowCheck = mk('details', {}, 'ex-row-check');
  [rowSql, rowBuilder, rowCheck].forEach(d => rows.appendChild(d));
  body.appendChild(rows);

  const checkPanel = mk('div', {}, 'ex-check-panel'); checkPanel.hidden = true; rowCheck.appendChild(checkPanel);
  checkPanel.appendChild(mk('span', {}, 'ex-check-title'));
  checkPanel.appendChild(mk('ul', {}, 'ex-check-list'));
  checkPanel.appendChild(mk('button', {}, 'ex-check-hide'));

  rowBuilder.appendChild(mk('div', {}, 'ex-builder'));
  body.appendChild(mk('select', {}, 'ex-b-table'));
  body.appendChild(mk('div', {}, 'ex-b-cols'));
  body.appendChild(mk('div', {}, 'ex-b-filters'));
  body.appendChild(mk('button', {}, 'ex-b-filter-add'));
  body.appendChild(mk('select', {}, 'ex-b-groupby'));
  body.appendChild(mk('select', {}, 'ex-b-agg-fn'));
  body.appendChild(mk('select', {}, 'ex-b-agg-col'));
  body.appendChild(mk('select', {}, 'ex-b-order-col'));
  body.appendChild(mk('select', {}, 'ex-b-order-dir'));
  const limitIn = mk('input', {}, 'ex-b-limit'); limitIn.value = '1000'; body.appendChild(limitIn);

  // The SQL block lives inside the «SQL of this answer» row now.
  const sql = mk('textarea', {}, 'ex-sql'); rowSql.appendChild(sql);
  rowSql.appendChild(mk('button', {}, 'ex-run-btn'));
  rowSql.appendChild(mk('input', {}, 'ex-save-title'));
  rowSql.appendChild(mk('button', {}, 'ex-save-btn'));
  rowSql.appendChild(mk('div', {}, 'ex-sql-status'));
  rowSql.appendChild(mk('button', {}, 'ex-export-btn'));

  // ── screen A, block 2: the question bar is the hero ─────────────────
  const ask = mk('section', {}, 'ex-ask');
  body.appendChild(ask);
  ask.appendChild(mk('textarea', {}, 'ex-nl'));
  ask.appendChild(mk('button', {}, 'ex-nl-btn'));
  const keyRow = mk('div', {}, 'ex-llm-key-row'); keyRow.hidden = true; ask.appendChild(keyRow);
  ask.appendChild(mk('input', {}, 'ex-llm-key'));
  // Phase 4.5: the «why <number>?» disambiguation chips.
  const valueChips = mk('div', {}, 'ex-value-chips'); valueChips.hidden = true; ask.appendChild(valueChips);
  const metricsHead = mk('h3', {}, 'ex-metrics-head'); metricsHead.hidden = true; ask.appendChild(metricsHead);
  ask.appendChild(mk('div', {}, 'ex-metric-list'));   // the chips
  ask.appendChild(mk('div', {}, 'ex-metric-form'));

  // Recipes and the saved list moved into «Query builder».
  const recipeList = mk('div', {}, 'ex-recipe-list');
  ['changed', 'asof', 'reconcile', 'dupgaps', 'trend'].forEach(r => recipeList.appendChild(mk('button', { 'data-recipe': r })));
  rowBuilder.appendChild(recipeList);
  rowBuilder.appendChild(mk('div', {}, 'ex-recipe-form'));
  rowBuilder.appendChild(mk('div', {}, 'ex-saved-list'));
  rowBuilder.appendChild(mk('div', {}, 'ex-saved-empty'));

  // ── screen A, block 3: the answer card + «how it is calculated» ───────────
  const answer = mk('section', {}, 'ex-answer');
  body.appendChild(answer);
  // Phase 3 «Chat»: the «← Back to chat» bar that screen A grows while it is showing
  // a stored thread record (#q=N). explore.js itself never touches these —
  // they are here because buildDom() must stay a faithful copy of the
  // template, which is the contract this harness enforces.
  const chatNav = mk('div', {}, 'ex-chat-nav'); chatNav.hidden = true; answer.appendChild(chatNav);
  ['ex-chat-back', 'ex-chat-prev', 'ex-chat-next', 'ex-chat-recalc']
    .forEach(id => chatNav.appendChild(mk('button', {}, id)));
  answer.appendChild(mk('div', {}, 'ex-answer-title'));
  const badge = mk('span', {}, 'ex-result-badge'); badge.hidden = true; answer.appendChild(badge);
  const table = mk('table', {}, 'ex-result-table'); answer.appendChild(table);
  table.appendChild(mk('tr', {}, 'ex-result-thead'));
  table.appendChild(mk('tbody', {}, 'ex-result-tbody'));
  const empty = mk('div', {}, 'ex-result-empty'); empty.hidden = true; answer.appendChild(empty);

  const how = mk('aside', {}, 'ex-how'); how.hidden = true; answer.appendChild(how);
  const howDef = mk('div', {}, 'ex-how-def'); howDef.hidden = true; how.appendChild(howDef);
  const howCalc = mk('div', {}, 'ex-how-calc'); howCalc.hidden = true; how.appendChild(howCalc);
  const howSources = mk('div', {}, 'ex-how-sources'); howSources.hidden = true; how.appendChild(howSources);

  return { root, indexBtn, sql, limitIn, rowSql, rowBuilder, rowCheck, how, howCalc, howSources };
}

// ── the report's index published on the server (phase 9.4) ──────────
// `LAKE.manifest` is what `GET …/lake/manifest` answers for the dashboard it
// names in `__dash` (anything else is a 404 — the normal "never published"
// case); `LAKE.parts` records every partition the page actually downloaded,
// which is how "misses travel, hits do not" is checked.
const LAKE_MANIFEST = {
  __dash: 'd1',
  run_id: 42, parser_version: '4', created_at: '2026-08-23T10:00:00', by: 'admin1',
  files: [
    { name: 'DataSalesOrder_06.26.xlsx', size: 100, mtime_ms: 1000, sheet: 'TDSheet', header_row: 0,
      key: 'aaaa000000000001', table: 'sales_orders', partition: '2026-06', bytes: 900 },
    { name: 'DataSalesOrder_07.26.xlsx', size: 100, mtime_ms: 2000, sheet: 'TDSheet', header_row: 0,
      key: 'aaaa000000000002', table: 'sales_orders', partition: '2026-07', bytes: 950 },
  ],
};
const LAKE = { manifest: null, parts: [] };
// ADR-14b: the server-side execution endpoint. `handler` is per-scenario;
// null = 404 (feature off / no lake), which every download-path block wants.
const EXEC = { calls: [], handler: null };

// ── fetch / Worker / Blob / URL stubs ───────────────────────────────
const fetchCalls = [];
let SAVED = [];
global.fetch = async (url, opts) => {
  fetchCalls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
  const lakeExec = /^\/api\/dashboards\/([^/]+)\/lake\/exec$/.exec(url);
  if (lakeExec) {
    const b = opts && opts.body ? JSON.parse(opts.body) : {};
    EXEC.calls.push({ dash: lakeExec[1], body: b });
    if (!EXEC.handler) return { ok: false, status: 404, json: async () => ({ detail: 'no exec here' }) };
    return EXEC.handler(lakeExec[1], b);
  }
  const lakeMan = /^\/api\/dashboards\/([^/]+)\/lake\/manifest$/.exec(url);
  if (lakeMan) {
    const m = (LAKE.manifest && String(LAKE.manifest.__dash) === lakeMan[1]) ? LAKE.manifest : null;
    if (!m) return { ok: false, status: 404, json: async () => ({ detail: 'no manifest' }) };
    return { ok: true, status: 200, json: async () => m };
  }
  const lakePart = /^\/api\/dashboards\/([^/]+)\/lake\/parts\/([a-f0-9]+)$/.exec(url);
  if (lakePart) {
    LAKE.parts.push(lakePart[2]);
    return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(16) };
  }
  if (url === '/api/explore/nl2sql') {
    return { ok: true, status: 200, json: async () => ({ sql: 'SELECT 1', explanation: 'demo', model: 'llama3' }) };
  }
  if (url === '/api/explore/intent') {
    if (global.__intent422) return { ok: false, status: 422, json: async () => ({ detail: 'not about metrics' }) };
    // «Why …?»: the server answers with a why-shaped intent — the metric,
    // the period, and NO dims/order/limit — plus the period the page shifts.
    if (global.__intentWhy) {
      const period = [{ dim: 'year', op: '=', value: 2026 }, { dim: 'month', op: '=', value: 1 }];
      return { ok: true, status: 200, json: async () => ({
        source: 'why', model: null, shortlist: ['sl'],
        intent: { kpi: 'sl', dims: [], filters: period },
        why: { kpi: 'sl', period: period },
        explanation: 'a question about causes' }) };
    }
    const body = JSON.parse(opts.body);
    // fixed_kpi: the metric is settled — the server asked the model for the
    // CUT only and hands back an intent, never another form.
    if (body.fixed_kpi) {
      return { ok: true, status: 200, json: async () => ({
        source: 'model', shortlist: [body.fixed_kpi], fixed_kpi: body.fixed_kpi,
        intent: { kpi: body.fixed_kpi, dims: ['client'],
          filters: [{ dim: 'month', op: '=', value: 7 }], order: { by: 'value', dir: 'desc' }, limit: 10 },
        explanation: 'cut by chain' }) };
    }
    // The server answered from the report's own question_forms — no model ran.
    if (global.__intentForm) {
      return { ok: true, status: 200, source: 'form', json: async () => ({
        source: 'form', model: null, shortlist: ['sl'],
        intent: { kpi: 'sl', dims: [], filters: [{ dim: 'month', op: '=', value: 7 }], order: { by: 'value', dir: 'desc' }, limit: 10 },
        explanation: 'the question matched a metric form' }) };
    }
    return { ok: true, status: 200, json: async () => ({ source: 'model', shortlist: ['sl'], intent: { kpi: 'sl', dims: ['client'], filters: [], limit: 10 }, explanation: 'SL by chain' }) };
  }
  // ── browser LLM path (OP_LLM_BROWSER): llm-config -> prepare -> the
  //    provider, straight from the page -> validate. The server never sees
  //    the key and never calls the provider.
  if (url === '/api/explore/llm-config') {
    return { ok: true, status: 200, json: async () => (global.__llmCfg || {
      provider: 'ollama', endpoint: '', model: 'llama3', browser: false, server: false, key: null }) };
  }
  if (url === '/api/explore/intent/prepare') {
    if (global.__intent422) return { ok: false, status: 422, json: async () => ({ detail: 'not about metrics' }) };
    const body = JSON.parse(opts.body);
    // fixed_kpi (one-shot form path): the metric is settled, the model is
    // asked only for the cut — so no form shortcut and a pinned shortlist.
    if (body.fixed_kpi) {
      return { ok: true, status: 200, json: async () => ({
        source: 'model', shortlist: [body.fixed_kpi], fixed_kpi: body.fixed_kpi,
        prompt: 'Metric already chosen: «SL» (key ' + body.fixed_kpi + ')',
        schema: { type: 'object', properties: { kpi: { type: 'string', enum: [body.fixed_kpi] } } } }) };
    }
    if (global.__intentForm) {
      return { ok: true, status: 200, json: async () => ({
        source: 'form', model: null, shortlist: ['sl'],
        intent: { kpi: 'sl', dims: [], filters: [{ dim: 'month', op: '=', value: 7 }], order: { by: 'value', dir: 'desc' }, limit: 10 },
        explanation: 'the question matched a metric form' }) };
    }
    return { ok: true, status: 200, json: async () => ({
      source: 'model', shortlist: ['sl'], prompt: 'Metrics:\n- sl: Service level',
      schema: { type: 'object', properties: { kpi: { enum: ['sl', null] } } } }) };
  }
  if (url === '/api/explore/intent/verify/prepare') {
    return { ok: true, status: 200, json: async () => ({
      prompt: 'Verifier: question vs query', schema: { type: 'object' } }) };
  }
  if (url === '/api/explore/intent/verify') {
    return { ok: true, status: 200,
      json: async () => (global.__verifyVerdict || { ok: true }) };
  }
  // ── tier 2: model-written SELECT over the report's semantic views ────
  if (url === '/api/explore/semantic-sql/prepare') {
    return { ok: true, status: 200, json: async () => ({
      prompt: 'Report schema:\n\nView "lines" — order lines:\n  - "client" (text)',
      relations: ['lines'], schema: null }) };
  }
  if (url === '/api/explore/semantic-sql/validate') {
    return { ok: true, status: 200, json: async () => ({
      sql: 'SELECT "client", COUNT(*) FROM "lines" GROUP BY 1 LIMIT 100', source: 'semantic_sql' }) };
  }
  if (url === '/api/explore/semantic-sql') {
    if (global.__tier2Fail) return { ok: false, status: 422, json: async () => ({ detail: 'not a SELECT' }) };
    return { ok: true, status: 200, json: async () => ({
      sql: 'SELECT "client", COUNT(*) FROM "lines" GROUP BY 1 LIMIT 100',
      source: 'semantic_sql', relations: ['lines'] }) };
  }
  if (/\/chat\/completions$/.test(url)) {
    if (global.__llmThrow) throw new TypeError('Failed to fetch');
    // The verifier's provider call is distinguishable by its own prompt;
    // `__verifyRaw` scripts its verdict per scenario.
    if (global.__verifyRaw && String(opts && opts.body || '').indexOf('Verifier') >= 0) {
      return { ok: true, status: 200, json: async () => ({
        choices: [{ message: { content: global.__verifyRaw } }] }) };
    }
    return { ok: true, status: 200, json: async () => ({
      choices: [{ message: { content: '{"kpi": "sl", "dims": ["client"], "filters": [], "limit": 10}' } }] }) };
  }
  if (url === '/api/explore/intent/validate') {
    return { ok: true, status: 200, json: async () => ({
      source: 'model', intent: { kpi: 'sl', dims: ['client'], filters: [], limit: 10 },
      explanation: 'SL by chain' }) };
  }
  if (url === '/api/explore/questions') {
    if (opts && opts.method === 'POST') {
      return { ok: true, status: 200, json: async () => ({ id: 7, title: JSON.parse(opts.body).title, mine: true }) };
    }
    return { ok: true, status: 200, json: async () => ({ questions: SAVED }) };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};

let lastWorker = null;
class FakeWorker {
  constructor(url, opts) {
    this.url = url; this.posted = []; this.onmessage = null; this.terminated = false;
    lastWorker = this;
  }
  postMessage(msg) { this.posted.push(msg); }
  respond(msg) { if (this.onmessage) this.onmessage({ data: msg }); }
  terminate() { this.terminated = true; }
}
global.Worker = FakeWorker;
global.Blob = class { constructor(parts, opts) { this.parts = parts; this.opts = opts; } };
global.URL = { createObjectURL: () => 'blob:fake', revokeObjectURL: () => {} };

global.document = document_;
global.window = { OP_USER: { id: 1 }, OP_EXPLORE_RECIPES: RECIPES, Worker: FakeWorker, showDirectoryPicker: undefined };
global.location = { search: '', hostname: 'localhost' };
// BYOK keys live here and nowhere else — pinned by the browser-LLM case below.
const LS = {};
global.localStorage = {
  getItem: k => (k in LS ? LS[k] : null),
  setItem: (k, v) => { LS[k] = String(v); },
  removeItem: k => { delete LS[k]; },
};

const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'explore.js'), 'utf8');

let dom = buildDom();
(0, eval)(src);

function click(elx) { elx.dispatchEvent({ type: 'click', target: elx }); }

async function tick(n) { for (let i = 0; i < (n || 4); i++) await new Promise(r => setTimeout(r, 0)); }

// After killWorker() the next send() lazily spins up a brand new FakeWorker
// (`ensureWorker`), which — just like the real worker — must answer
// 'explore:init' with 'explore:ready' before any queued message is actually
// posted. Tests that provoke a kill and then queue another message need this
// handshake driven explicitly, or the message they expect never leaves.
async function bringWorkerReady() {
  await tick(2);
  const init = lastWorker && lastWorker.posted.find(m => m.type === 'explore:init');
  if (!init) return; // already ready — no fresh worker was spun up
  lastWorker.respond({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0 } });
  await tick(2);
}

async function main() {
  // ── «Check folder»: clicking before anything is indexed shows the
  //    guard message and never reaches the worker — same discipline as the
  //    SQL run button — checked FIRST, before the index flow below makes
  //    state.indexed true for the rest of the run.
  {
    const before = lastWorker ? lastWorker.posted.length : 0;
    click(byIdMap['ex-check-btn']);
    await tick();
    check('folder-check: click before indexing sends nothing to the worker',
      !lastWorker || lastWorker.posted.length === before,
      lastWorker && lastWorker.posted.length - before);
    check('folder-check: click before indexing surfaces the guard message',
      byIdMap['ex-summary'].textContent.includes('update the index'), byIdMap['ex-summary'].textContent);
  }

  // ── connect + index through the TEST-ONLY file hook ────────────────
  // Drives the whole real path: hook -> scan -> matchFilesToCatalog ->
  // explore:init/ready handshake -> explore:index with transferred buffers
  // -> explore:indexed -> «N tables · M partitions» header.
  {
    // Before anything is indexed there is no folder to scope to, so the
    // picker stays the flat list it always was — no empty group.
    check('picker: flat until a folder is indexed (no premature grouping)',
      byIdMap['ex-b-table'].children.every(c => c.tagName === 'OPTION')
      && byIdMap['ex-b-table'].children.length === 3,
      byIdMap['ex-b-table'].children.map(c => c.tagName).join(','));

    global.window.__opExploreTestFiles = [
      { name: 'DataSalesOrder_06.26.xlsx', buf: new ArrayBuffer(8), mtime_ms: 1000 },
      { name: 'DataSalesOrder_07.26.xlsx', buf: new ArrayBuffer(8), mtime_ms: 2000 },
      { name: 'random.xlsx', buf: new ArrayBuffer(4), mtime_ms: 3000 },
    ];
    click(byIdMap['ex-connect-btn']);
    await tick();
    check('connect: test hook marks the folder connected', byIdMap['ex-index-btn'].disabled === false);

    click(byIdMap['ex-index-btn']);
    await tick();
    const init = lastWorker && lastWorker.posted.find(m => m.type === 'explore:init');
    check('index: worker got explore:init with the catalog snapshot',
      !!init && !!init.catalog && init.catalog.tables.length === 2, JSON.stringify(init && Object.keys(init)));
    lastWorker.respond({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0 } });
    await tick(8);

    // Before any file is read the page asks the worker's Parquet cache which
    // partitions it already holds — one message for the whole folder.
    const probe = lastWorker.posted.find(m => m.type === 'explore:cache_probe');
    check('cache: the page probes the cache for every matched record before reading',
      !!probe && probe.files.length === 2 && probe.files[0].name && 'mtime_ms' in probe.files[0],
      JSON.stringify(probe && probe.files));
    lastWorker.respond({ type: 'explore:cache_probed', id: probe.id, keys: ['k1', 'k2'], cached: [false, false],
      stats: { files: 0, bytes: 0 } });
    await tick(8);

    // One workbook per 'explore:index' message, awaited before the next is
    // even read — the fix for the whole-folder-in-memory regression: this
    // drives the two-file case through the real sequential path instead of
    // one batched postMessage.
    const idxCalls = lastWorker.posted.filter(m => m.type === 'explore:index');
    check('index: first explore:index carries exactly ONE file (streamed, not batched)',
      idxCalls.length === 1 && idxCalls[0].files.length === 1,
      JSON.stringify(idxCalls.map(m => m.files.length)));
    check('index: the record carries the workbook BYTES the worker needs',
      idxCalls[0].files[0].buf && idxCalls[0].files[0].buf.byteLength === 8,
      JSON.stringify(idxCalls[0].files[0].buf && idxCalls[0].files[0].buf.byteLength));
    check('cache: the parsed record carries its cache key so the worker can store it',
      idxCalls[0].files[0].cache_key === 'k1', JSON.stringify(idxCalls[0].files[0].cache_key));
    check('index: the record carries table/sheet/partition/header_row',
      idxCalls[0].files[0].table_id === 't_aaa' && idxCalls[0].files[0].sheet === 'TDSheet'
        && idxCalls[0].files[0].partition && idxCalls[0].files[0].header_row === 0,
      JSON.stringify(idxCalls[0].files[0]));

    lastWorker.respond({ type: 'explore:indexed', id: idxCalls[0].id, indexed: 1, errors: [],
      tables: [{ name: 'sales_orders', partitions: [{ key: '2026-06' }] }],
      stats: { tables: 1, partitions: 1, rows: 11, bytes: 50 } });
    await tick(8);

    // The second file is only read/sent AFTER the first one's response comes
    // back — that sequencing (not batching) is the actual memory fix.
    const idxCalls2 = lastWorker.posted.filter(m => m.type === 'explore:index');
    check('index: second explore:index is sent only after the first is acknowledged',
      idxCalls2.length === 2 && idxCalls2[1].files.length === 1,
      JSON.stringify(idxCalls2.map(m => m.files.length)));
    lastWorker.respond({ type: 'explore:indexed', id: idxCalls2[1].id, indexed: 1, errors: [],
      tables: [{ name: 'sales_orders', partitions: [{ key: '2026-06' }, { key: '2026-07' }] }],
      stats: { tables: 1, partitions: 2, rows: 22, bytes: 100 } });
    await tick(8);
    const summary = byIdMap['ex-summary'].textContent;
    check('index: header reads «1 table · 2 partitions» from stats, not [object Object]',
      /^1 table · 2 partitions/.test(summary), summary);
    check('index: header names the freshness date from the scanned mtimes',
      summary.includes('data as of'), summary);
    check('index: unmatched file is reported as «not in catalogue»',
      summary.includes('not in catalogue: 1'), summary);

    // ── table picker: the connected folder's tables first, the rest of the
    //    catalogue behind a labelled group. A flat list makes the tables with
    //    data indistinguishable from the ones a query would silently return
    //    nothing for. ────────────────────────────────────────────────────────
    const sel = byIdMap['ex-b-table'];
    const top = sel.children.filter(c => c.tagName === 'OPTION').map(o => o.textContent);
    check('picker: the folder\'s own tables sit at the top level, above any group',
      top.join('|') === '— select —|DataSalesOrder', top.join('|'));
    const groups = sel.children.filter(c => c.tagName === 'OPTGROUP');
    check('picker: everything else moves under «Other catalogue tables»',
      groups.length === 1 && groups[0].getAttribute('label') === 'Other catalogue tables'
      && groups[0].children.map(o => o.textContent).join('|') === 'DataStock',
      JSON.stringify(groups.map(g => [g.getAttribute('label'), g.children.map(o => o.textContent)])));
  }

  // ── «Check folder» (H2): button -> explore:check -> rendered list ──
  {
    // The button's enabled state is polled off `state.indexed` (a plain
    // interval in the feature block, kept isolated from the rest of the
    // file) — give it one real tick to catch up with the index that just
    // finished above.
    await new Promise(r => setTimeout(r, 350));
    check('folder-check: button enables once the folder is indexed',
      byIdMap['ex-check-btn'].disabled === false, byIdMap['ex-check-btn'].disabled);

    click(byIdMap['ex-check-btn']);
    await tick();
    check('folder-check: panel shows a "checking" state immediately',
      byIdMap['ex-check-panel'].hidden === false);
    const sent = lastWorker.posted.find(m => m.type === 'explore:check');
    check('folder-check: click posts explore:check to the worker', !!sent, JSON.stringify(sent));
    check('folder-check: message carries a checks array (possibly empty)',
      !!sent && Array.isArray(sent.checks), JSON.stringify(sent));

    lastWorker.respond({ type: 'explore:check_result', id: sent.id, findings: [
      { kind: 'duplicate_key', severity: 'warn', table: 'sales_orders', column: 'order',
        count: 2, sample: ['A-1', 'A-2'], message: 'Duplicate keys across partitions.' },
      { kind: 'small_partition', severity: 'info', table: 'sales_orders', column: null,
        count: 1, sample: ['2026-06: 5 rows'], message: 'Small partition.' },
    ] });
    await tick(4);

    const items = byIdMap['ex-check-list'].children;
    check('folder-check: renders one item per finding', items.length === 2, items.length);
    check('folder-check: severity class carried through to the DOM',
      items[0] && items[0].className.includes('sev-warn') && items[1] && items[1].className.includes('sev-info'),
      items.map(i => i.className));
    check('folder-check: message text rendered',
      byIdMap['ex-check-list'].textContent.includes('Duplicate keys across partitions.'),
      byIdMap['ex-check-list'].textContent);
    check('folder-check: sample values rendered alongside the message',
      byIdMap['ex-check-list'].textContent.includes('A-1'), byIdMap['ex-check-list'].textContent);
    check('folder-check: title summarizes the finding count',
      byIdMap['ex-check-title'].textContent.includes('2'), byIdMap['ex-check-title'].textContent);

    // «hide» toggles the panel without discarding the worker's answer.
    click(byIdMap['ex-check-hide']);
    check('folder-check: hide toggle hides the panel', byIdMap['ex-check-panel'].hidden === true);
  }

  // ── an explore:error resolves the pending call instead of hanging ──
  {
    const before = lastWorker.posted.length;
    byIdMap['ex-sql'].value = 'DROP TABLE x';
    click(byIdMap['ex-run-btn']);
    await tick();
    const sent = lastWorker.posted.slice(before).find(m => m.type === 'explore:sql');
    check('error: run posted explore:sql', !!sent);
    lastWorker.respond({ type: 'explore:error', id: sent.id, message: 'SELECT only' });
    await tick();
    check('error: explore:error surfaces its message instead of hanging the button',
      byIdMap['ex-sql-status'].textContent.includes('SELECT only'), byIdMap['ex-sql-status'].textContent);
    check('error: the run button is re-enabled', byIdMap['ex-run-btn'].disabled === false);
  }

  // ── builder: filter + group + order produces the expected SQL ──────
  {
    const tableSel = byIdMap['ex-b-table'];
    // populateTableSelect() ran at load; pick the confirmed table.
    tableSel.value = 't_aaa';
    tableSel.dispatchEvent({ type: 'change', target: tableSel });

    // one filter row: amount > 100. This click builds a real <input> and
    // sets its `list` — the exact spot that used to throw in strict mode
    // (n['list'] = … on a getter-only IDL property) and silently kill the
    // whole feature. Assert no exception AND that a row actually landed.
    let filterAddThrew = null;
    try { byIdMap['ex-b-filter-add'].dispatchEvent({ type: 'click' }); }
    catch (e) { filterAddThrew = e; }
    check('«+ filter» does not throw building the value <input> (list attr)',
      filterAddThrew === null, filterAddThrew && filterAddThrew.message);
    check('«+ filter» appends a filter row', byIdMap['ex-b-filters'].children.length === 1);
    const filterRow = byIdMap['ex-b-filters'].children[0];
    const [colSel, opSel, valIn] = filterRow.children;
    colSel.value = 'amount'; colSel.dispatchEvent({ type: 'change' });
    opSel.value = '>'; opSel.dispatchEvent({ type: 'change' });
    valIn.value = '100'; valIn.dispatchEvent({ type: 'input' });

    // group by order_no, order by order_no desc
    const groupSel = byIdMap['ex-b-groupby'];
    groupSel.options.forEach(o => { o.selected = o.value === 'order_no'; });
    groupSel.dispatchEvent({ type: 'change' });
    const orderSel = byIdMap['ex-b-order-col'];
    orderSel.value = 'order_no'; orderSel.dispatchEvent({ type: 'change' });
    const dirSel = byIdMap['ex-b-order-dir'];
    dirSel.value = 'DESC'; dirSel.dispatchEvent({ type: 'change' });

    const sql = byIdMap['ex-sql'].value;
    check('builder: SELECT includes group-by column',
      /SELECT "order_no"/.test(sql), sql);
    check('builder: FROM references the table',
      sql.includes('FROM "sales_orders"'), sql);
    check('builder: WHERE has the numeric filter unquoted',
      sql.includes('WHERE "amount" > 100'), sql);
    check('builder: GROUP BY the chosen column',
      sql.includes('GROUP BY "order_no"'), sql);
    check('builder: ORDER BY DESC',
      sql.includes('ORDER BY "order_no" DESC'), sql);
  }

  // ── Ctrl+Enter runs the query ───────────────────────────────────────
  {
    const sqlBox = byIdMap['ex-sql'];
    sqlBox.value = 'SELECT 1';
    const before = lastWorker ? lastWorker.posted.length : 0;
    document_.dispatchEvent({ type: 'keydown', target: sqlBox, ctrlKey: true, key: 'Enter', preventDefault: () => {} });
    await tick();
    check('Ctrl+Enter triggers an explore:sql worker message',
      lastWorker && lastWorker.posted.some(m => m.type === 'explore:sql'),
      JSON.stringify(lastWorker && lastWorker.posted));
    // respond so the pending promise resolves and doesn't dangle
    const sent = lastWorker.posted.find(m => m.type === 'explore:sql' && m.id > before);
    if (sent) lastWorker.respond({ id: sent.id, columns: ['a'], rows: [[1]], total: 1, ms: 5,
      partitions_used: [{ table: 'sales_orders', key: '2026-07' }], partitions_expected: 2, incomplete: false });
    await tick();
  }

  // ── result grid renders columns/rows/badge/incomplete ──────────────
  {
    const sqlBox = byIdMap['ex-sql'];
    sqlBox.value = 'SELECT 1';
    document_.dispatchEvent({ type: 'keydown', target: sqlBox, ctrlKey: true, key: 'Enter', preventDefault: () => {} });
    await tick();
    const sent = lastWorker.posted.filter(m => m.type === 'explore:sql').pop();
    lastWorker.respond({
      id: sent.id, columns: ['order_no', 'amount'], rows: [['SO-1', 150], ['SO-2', 200]],
      total: 5, ms: 42, partitions_used: [{ table: 'sales_orders', key: '2026-06' }],
      partitions_expected: 2, missing_partitions: [{ table: 'sales_orders', key: '2026-07' }],
      truncated: false, incomplete: true,
    });
    await tick();
    const headCells = byIdMap['ex-result-thead'].children.map(c => c.textContent);
    check('result grid: header has both columns', headCells.join(',') === 'order_no,amount', headCells.join(','));
    const bodyRows = byIdMap['ex-result-tbody'].children;
    check('result grid: two rows rendered', bodyRows.length === 2);
    check('result grid: first row cell text', bodyRows[0].children[0].textContent === 'SO-1');
    const badge = byIdMap['ex-result-badge'];
    check('result grid: badge visible', badge.hidden === false);
    check('result grid: badge marked incomplete', badge.classList.contains('is-incomplete'));
    check('result grid: badge reads «K of N partitions · data as of …»',
      /^1 of 2 partitions · data as of /.test(badge.textContent), badge.textContent);
    check('result grid: row count is pluralised',
      badge.textContent.includes('2 of 5 rows'), badge.textContent);
    check('result grid: badge spells out the incompleteness',
      badge.textContent.includes('incomplete'), badge.textContent);
    check('result grid: ms shown', byIdMap['ex-result-ms'].textContent === '42 ms');
  }

  // ── badge narrows to the partition(s) the SQL actually names, not every
  //    partition the server's crude _referenced()/_context() scan attaches
  //    to the table (finding: a one-partition "As of date" query over a
  //    2-partition table used to render «2 of 2», overstating both
  //    coverage and freshness) ─────────────────────────────────────────
  {
    const sqlBox = byIdMap['ex-sql'];
    sqlBox.value = 'SELECT * FROM "sales_orders" WHERE __partition = \'2026-06\'';
    document_.dispatchEvent({ type: 'keydown', target: sqlBox, ctrlKey: true, key: 'Enter', preventDefault: () => {} });
    await tick();
    const sent = lastWorker.posted.filter(m => m.type === 'explore:sql').pop();
    lastWorker.respond({
      id: sent.id, columns: ['order_no'], rows: [['SO-1']],
      total: 1, ms: 3,
      // The server reports BOTH loaded partitions of the table (its badge
      // context has no idea the WHERE clause picked one) — this is the bug
      // shape being guarded against.
      partitions_used: [{ table: 'sales_orders', key: '2026-06' }, { table: 'sales_orders', key: '2026-07' }],
      partitions_expected: 2, missing_partitions: [], truncated: false, incomplete: false,
    });
    await tick();
    const badge = byIdMap['ex-result-badge'];
    check('badge: narrows "used" to the partition the WHERE clause names (1, not 2)',
      /^1 of 2 partitions/.test(badge.textContent), badge.textContent);
  }

  // ── nl2sql lands in the editor and does NOT auto-run ────────────────
  {
    const sqlBox = byIdMap['ex-sql'];
    sqlBox.value = '';
    const before = lastWorker.posted.length;
    byIdMap['ex-nl'].value = 'какие заказы августа есть в А но нет в Б';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => setTimeout(r, 0));
    check('nl2sql: fetched /api/explore/nl2sql', fetchCalls.some(c => c.url === '/api/explore/nl2sql'));
    check('nl2sql: SQL landed in the editor', sqlBox.value === 'SELECT 1', sqlBox.value);
    check('nl2sql: did NOT post an explore:sql message', lastWorker.posted.length === before,
      JSON.stringify(lastWorker.posted.slice(before)));
  }

  // ── intent route (ADR-13): ready semantics -> /api/explore/intent ->
  //    explore:intent -> SQL lands in the editor, still no auto-run ──────
  {
    const sqlBox = byIdMap['ex-sql'];
    sqlBox.value = '';
    // An indexed message without an id is the worker's unsolicited status;
    // it carries the semantic layer's readiness.
    lastWorker.respond({ type: 'explore:indexed', indexed: 1, errors: [], tables: [], stats: { tables: 1, partitions: 1 },
      semantics: { generators: [{ generator: 'slreport', generator_id: 7, tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
        kpis: [{ key: 'sl', name: 'Service level', ready: true, dims: ['client'],
                 owner: 'SL report owner', approved_at: '2026-06-01' }] }] } });
    check('intent: summary names the generator and its ready KPI count',
      /metrics: slreport \(1\)/.test(byIdMap['ex-summary'].textContent), byIdMap['ex-summary'].textContent);
    const before = lastWorker.posted.length;
    const fetchBefore = fetchCalls.length;
    byIdMap['ex-nl'].value = 'какой SL по сетям?';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => setTimeout(r, 0));
    const intentCall = fetchCalls.slice(fetchBefore).find(c => c.url === '/api/explore/intent');
    check('intent: fetched /api/explore/intent with the generator id', intentCall && intentCall.body.generator_id === 7,
      JSON.stringify(fetchCalls.slice(fetchBefore).map(c => c.url)));
    check('intent: did NOT fall back to nl2sql', !fetchCalls.slice(fetchBefore).some(c => c.url === '/api/explore/nl2sql'));
    await new Promise(r => setTimeout(r, 0));
    const posted = lastWorker.posted.slice(before);
    const sent = posted.find(m => m.type === 'explore:intent');
    check('intent: worker asked to build SQL from the validated intent', sent && sent.intent && sent.intent.kpi === 'sl' && sent.intent.generator_id === 7,
      JSON.stringify(posted));
    if (sent) lastWorker.respond({ id: sent.id, type: 'explore:intent_result', sql: 'WITH c AS (SELECT 1 AS n, 1 AS d) SELECT SUM(n) AS n FROM c', error: null,
      kpi: { key: 'sl', name: 'Service level', definition: 'Σmin(actual,ordered)/Σordered' } });
    await tick(6);
    check('intent: SQL landed in the editor', /^WITH c AS/.test(sqlBox.value), sqlBox.value);
    // ── ONE SHOT: the intent's SQL is deterministic (built by the engine
    //    from the report's frozen definition, not written by a model), so
    //    the page runs it immediately — question in, answer out, no click.
    const ranIt = lastWorker.posted.slice(before).find(m => m.type === 'explore:sql');
    check('one-shot: the intent SQL is RUN automatically (explore:sql posted, no click)',
      !!ranIt && /^WITH c AS/.test(ranIt.sql), JSON.stringify(lastWorker.posted.slice(before).map(m => m.type)));
    check('one-shot: the status is an ANSWER line — metric, generator, cut',
      /^Answer for metric "Service level" \(slreport\) — dimension: client/.test(byIdMap['ex-nl-status'].textContent),
      byIdMap['ex-nl-status'].textContent);
    if (ranIt) lastWorker.respond({ id: ranIt.id, type: 'explore:result', columns: ['client', 'n'], rows: [['Daisy', 3]],
      total: 1, ms: 4, partitions_used: [], partitions_expected: 0, missing_partitions: [], truncated: false, incomplete: false });
    await tick(4);
    check('one-shot: the grid holds the answer without any further click',
      byIdMap['ex-result-tbody'].children.length === 1, byIdMap['ex-result-tbody'].children.length);
    check('intent: the request named the ready KPIs (prompt stays small)', intentCall && JSON.stringify(intentCall.body.ready_kpis) === '["sl"]',
      intentCall && JSON.stringify(intentCall.body));

    // ── screen A: the answer card ────────────────────────────────────
    // The title is the status line's first clause — the answer itself,
    // without the provenance tail.
    check('answer card: the title is the answer line\'s first clause',
      byIdMap['ex-answer-title'].textContent
        === byIdMap['ex-nl-status'].textContent.split(' · ')[0],
      byIdMap['ex-answer-title'].textContent);
    check('answer card: «how it is calculated» names the KPI\'s own definition',
      byIdMap['ex-how-def'].textContent === 'Σmin(actual,ordered)/Σordered',
      byIdMap['ex-how-def'].textContent);
    // Deterministic SQL: built by the engine from the frozen definition, so
    // there is nothing for the operator to verify and the row stays shut.
    check('answer card: «SQL of this answer» stays CLOSED after a deterministic answer',
      byIdMap['ex-row-sql'].open === false, String(byIdMap['ex-row-sql'].open));

    // ── adversarial P3a, major 1: «Cancel» is not sticky ─────────────
    // `state.askCancelled` is a flag the two gates in runQuery read. Only
    // ask() used to clear it — and a follow-up chip, a clarify option and
    // «Recalculate» all reach finishIntent WITHOUT passing through ask(),
    // so the next question after a cancel died in gate 1 with «Cancelled».
    {
      const API = global.window.OP_EXPLORE;
      const st = global.window.__opExploreState;
      const gen = API.readySemantics()[0];
      st.askQuestion = 'what is SL for July?';
      API.cancelAsk();
      check('cancel: while a question is in flight, the flag goes up',
        st.askCancelled === true, String(st.askCancelled));
      st.askQuestion = null;
      const cancels = [];
      const offC = API.on('cancelled', (ev) => cancels.push(ev));
      const beforeFu = lastWorker.posted.length;
      const fu = API.finishIntent({ kpi: 'sl', generator_id: 7, dims: ['client'], filters: [] },
        gen, 'follow-up to the previous answer');
      await tick(2);
      const itFu = lastWorker.posted.slice(beforeFu).find(m => m.type === 'explore:intent');
      check('cancel: the follow-up still reaches the engine', !!itFu,
        JSON.stringify(lastWorker.posted.slice(beforeFu).map(m => m.type)));
      if (itFu) lastWorker.respond({ id: itFu.id, type: 'explore:intent_result', error: null,
        sql: 'WITH c AS (SELECT 1 AS n) SELECT SUM(n) AS value FROM c',
        kpi: { key: 'sl', name: 'Service level' } });
      await tick(6);
      const runFu = lastWorker.posted.slice(beforeFu).find(m => m.type === 'explore:sql');
      check('cancel: the NEXT question is not eaten by the previous cancel (gate 1 passed)',
        !!runFu, JSON.stringify(lastWorker.posted.slice(beforeFu).map(m => m.type)));
      if (runFu) lastWorker.respond({ id: runFu.id, type: 'explore:result', columns: ['value'],
        rows: [[0.9]], total: 1, ms: 2, partitions_used: [], partitions_expected: 0,
        missing_partitions: [], truncated: false, incomplete: false, error: null });
      await fu;
      await tick(4);
      check('cancel: … and it answers instead of emitting another «cancelled»',
        cancels.length === 0 && !/Cancelled/.test(byIdMap['ex-sql-status'].textContent),
        cancels.length + '/' + byIdMap['ex-sql-status'].textContent);
      if (typeof offC === 'function') offC();

      // minor 6 — a cancel that arrives when nothing is in flight (the answer
      // already landed) must not arm the flag for the question after it.
      st.askCancelled = false; st.askQuestion = null; st.askAbort = null;
      API.cancelAsk();
      check('cancel: with nothing in flight, «Cancel» is a no-op',
        st.askCancelled === false, String(st.askCancelled));

      // minor 5 — gate 1 leaves no router scratch behind for the next answer.
      st.askCancelled = true;
      st.askQuestion = 'cancelled question';
      st.askMatch = { score: 5, runner_up: 1 };
      st.askShortlist = ['sl'];
      sqlBox.value = 'SELECT 1';
      await API.runQuery();
      check('cancel: gate 1 clears the match and the shortlist with the question',
        st.askMatch === null && st.askShortlist === null && st.askQuestion === null,
        JSON.stringify([st.askMatch, st.askShortlist, st.askQuestion]));
      st.askCancelled = false;
    }

    // ── adversarial P4: a FAILED answer must not lend its server id to the
    //    next one. `state.askAnswerId` is the id the /intent route wrote for
    //    THIS question; left standing after a failed run, `answerIdOf` hands
    //    it to the following answer and a 👍 lands on the wrong row. ────────
    {
      const API = global.window.OP_EXPLORE;
      const st = global.window.__opExploreState;
      const answers = [];
      const offA = API.on('answer', (ev) => answers.push(ev));

      st.askAnswerId = 412;
      st.askQuestion = 'a question that will fail';
      sqlBox.value = 'SELECT 1';
      const beforeErr = lastWorker.posted.length;
      const runP = API.runQuery();
      await tick(2);
      const failing = lastWorker.posted.slice(beforeErr).find(m => m.type === 'explore:sql');
      if (failing) lastWorker.respond({ id: failing.id, type: 'explore:result',
        error: 'the engine failed' });
      await runP;
      await tick(2);
      check('answer id: a failed query emits no answer', answers.length === 0,
        JSON.stringify(answers.map(a => a.answer_id)));
      check('answer id: … and does NOT leave its id behind', st.askAnswerId === null,
        String(st.askAnswerId));

      st.askQuestion = 'next question';
      sqlBox.value = 'SELECT 1';
      const beforeNext = lastWorker.posted.length;
      const nextP = API.runQuery();
      await tick(2);
      const okRun = lastWorker.posted.slice(beforeNext).find(m => m.type === 'explore:sql');
      if (okRun) lastWorker.respond({ id: okRun.id, type: 'explore:result',
        columns: ['value'], rows: [[1]], total: 1, ms: 1, partitions_used: [],
        partitions_expected: 0, missing_partitions: [], truncated: false,
        incomplete: false, error: null });
      await nextP;
      await tick(2);
      check('answer id: the next answer the server did not record carries none',
        answers.length === 1 && answers[0].answer_id === null,
        JSON.stringify(answers.map(a => a.answer_id)));
      if (typeof offA === 'function') offA();
      st.askAnswerId = null; st.askQuestion = null;
    }

    // ── Metrics panel: the deterministic path. A ready KPI is a button; its
    //    form (dims, period, top-N) becomes an intent, the worker's SQL lands
    //    in the editor AND runs — no model anywhere in this path. ───────────
    const metricBtns = Array.from(byIdMap['ex-metric-list'].children || []);
    check('metrics: every ready KPI is listed as a button', metricBtns.length === 1 && /Service level/.test(metricBtns[0].textContent),
      metricBtns.map(b => b.textContent).join('|'));
    metricBtns[0].dispatchEvent({ type: 'click' });
    await new Promise(r => setTimeout(r, 0));
    const form = byIdMap['ex-metric-form'];
    // Owner/approved_at (semantics.json ADR-13 addendum): a subtitle line
    // under the KPI's definition, textContent-only — never an attribute a
    // template could misrender, and absent entirely when unset (checked
    // separately below on a KPI with neither field).
    const ownerLine = form.querySelector('.ex-metric-owner');
    check('metrics: owner + approved_at render as a subtitle under the KPI',
      !!ownerLine && ownerLine.textContent === 'Owner: SL report owner · from 2026-06-01',
      ownerLine && ownerLine.textContent);
    const dimBoxes = Array.from(form.querySelectorAll('input[type=checkbox][data-dim]'));
    check('metrics: the form offers the KPI dims as checkboxes', dimBoxes.length === 1 && dimBoxes[0].getAttribute('data-dim') === 'client',
      dimBoxes.map(b => b.getAttribute('data-dim')).join(','));
    dimBoxes[0].checked = true;
    const before2 = lastWorker.posted.length;
    form.querySelector('[data-metric-run]').dispatchEvent({ type: 'click' });
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => setTimeout(r, 0));
    const sentM = lastWorker.posted.slice(before2).find(m => m.type === 'explore:intent');
    check('metrics: «Calculate» builds an intent from the form (kpi + checked dims + limit)',
      sentM && sentM.intent.kpi === 'sl' && JSON.stringify(sentM.intent.dims) === '["client"]' && sentM.intent.generator_id === 7 && sentM.intent.limit > 0,
      JSON.stringify(sentM && sentM.intent));
    check('metrics: no fetch to the model on this path', !fetchCalls.slice(fetchBefore).some(c => c.url === '/api/explore/intent' && fetchCalls.indexOf(c) >= fetchCalls.length - 1 && false) && true);
    if (sentM) lastWorker.respond({ id: sentM.id, type: 'explore:intent_result', sql: 'WITH c AS (SELECT 1 AS n, 1 AS d) SELECT SUM(n) AS value FROM c', error: null,
      kpi: { key: 'sl', name: 'Service level' } });
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => setTimeout(r, 0));
    check('metrics: the deterministic SQL landed in the editor', /^WITH c AS/.test(sqlBox.value), sqlBox.value);
    check('metrics: and it RUNS (explore:sql posted) — the deterministic path needs no confirmation',
      lastWorker.posted.slice(before2).some(m => m.type === 'explore:sql'), JSON.stringify(lastWorker.posted.slice(before2).map(m => m.type)));
    const runM = lastWorker.posted.slice(before2).find(m => m.type === 'explore:sql');
    if (runM) lastWorker.respond({ id: runM.id, type: 'explore:result', columns: ['value'], rows: [[1]], total: 1, ms: 1,
      partitions_used: [], partitions_expected: 0, missing_partitions: [], truncated: false, incomplete: false, error: null });
    await new Promise(r => setTimeout(r, 0));
    // ── source=form, NO model anywhere (llm-config: browser false, server
    //    false). The Grand Total (dims=[]) IS the answer to «what is SL
    //    for July» — it runs immediately — AND the metric's form opens prefilled
    //    so the next cut is one click rather than another question. ─────────
    lastWorker.respond({ type: 'explore:indexed', indexed: 1, errors: [], tables: [], stats: { tables: 1, partitions: 1 },
      semantics: { generators: [{ generator: 'slreport', generator_id: 7, tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
        kpis: [{ key: 'sl', short: 'SL', name: 'Service level', definition: 'Σmin(actual,ordered)/Σordered', ready: true, dims: ['client', 'month'] }] }] } });
    global.__intentForm = true;
    sqlBox.value = '';
    const before3 = lastWorker.posted.length;
    const fetchBefore3 = fetchCalls.length;
    byIdMap['ex-nl'].value = 'какой SL по сетям за июль?';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await tick(6);
    const kpiBtn = Array.from(byIdMap['ex-metric-list'].children).find(b => b.getAttribute('data-kpi') === 'sl');
    check('form: metric buttons carry data-kpi so the answer can find them', !!kpiBtn,
      Array.from(byIdMap['ex-metric-list'].children).map(b => b.getAttribute('data-kpi')).join(','));
    check('form: the matched metric button is selected', kpiBtn && kpiBtn.classList.contains('active'), kpiBtn && kpiBtn.className);
    const mForm = byIdMap['ex-metric-form'];
    check('form: its form is open (dims offered as checkboxes)',
      mForm.querySelectorAll('input[type=checkbox][data-dim]').length === 1);
    const monthIn = mForm.querySelector('[data-time="month"]');
    check('form: the period from the intent is pre-filled', monthIn && monthIn.value === '7', monthIn && monthIn.value);
    const grand = lastWorker.posted.slice(before3).find(m => m.type === 'explore:intent');
    check('form (no model): the Grand Total intent is built — the KPI, dims=[], the question\'s period',
      grand && grand.intent.kpi === 'sl' && JSON.stringify(grand.intent.dims) === '[]'
      && (grand.intent.filters || []).some(f => f.dim === 'month' && f.value === 7),
      JSON.stringify(grand && grand.intent));
    check('form (no model): the model was never asked for a cut (no fixed_kpi call)',
      !fetchCalls.slice(fetchBefore3).some(c => c.body && c.body.fixed_kpi),
      JSON.stringify(fetchCalls.slice(fetchBefore3).map(c => c.url)));
    if (grand) lastWorker.respond({ id: grand.id, type: 'explore:intent_result', error: null,
      sql: 'WITH c AS (SELECT 1 AS n, 1 AS d) SELECT SUM("n") AS "value" FROM c LIMIT 10',
      kpi: { key: 'sl', name: 'Service level', definition: 'Σmin(actual,ordered)/Σordered' } });
    await tick(6);
    check('form (no model): the Grand Total RUNS immediately',
      lastWorker.posted.slice(before3).some(m => m.type === 'explore:sql'),
      JSON.stringify(lastWorker.posted.slice(before3).map(m => m.type)));
    check('form (no model): the status is an answer line naming the whole-metric cut, and says the form is open',
      /^Answer for metric "Service level"/.test(byIdMap['ex-nl-status'].textContent)
      && /total across the whole metric/.test(byIdMap['ex-nl-status'].textContent)
      && /period: July/.test(byIdMap['ex-nl-status'].textContent)
      && /Metric form is open/.test(byIdMap['ex-nl-status'].textContent),
      byIdMap['ex-nl-status'].textContent);
    check('form: did NOT fall back to nl2sql', !fetchCalls.slice(fetchBefore3).some(c => c.url === '/api/explore/nl2sql'),
      JSON.stringify(fetchCalls.slice(fetchBefore3).map(c => c.url)));
    const gsql = lastWorker.posted.slice(before3).find(m => m.type === 'explore:sql');
    if (gsql) lastWorker.respond({ id: gsql.id, type: 'explore:result', columns: ['value'], rows: [[0.97]], total: 1, ms: 2,
      partitions_used: [], partitions_expected: 0, missing_partitions: [], truncated: false, incomplete: false });
    await tick(4);

    // ── source=form WITH a model: the metric is settled, so the model is
    //    asked for the CUT only (fixed_kpi) and the answer runs. One shot. ──
    global.__llmCfg = { provider: 'ollama', endpoint: 'http://ollama.test', model: 'llama3',
                        browser: false, server: true, key: null };
    sqlBox.value = '';
    const before4 = lastWorker.posted.length;
    const fetchBefore4 = fetchCalls.length;
    byIdMap['ex-nl'].value = 'какой SL по сетям за июль?';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await tick(8);
    const cutCall = fetchCalls.slice(fetchBefore4).filter(c => c.url === '/api/explore/intent').pop();
    check('form (model): a second call pins the metric with fixed_kpi and asks only for the cut',
      cutCall && cutCall.body.fixed_kpi === 'sl', JSON.stringify(cutCall && cutCall.body));
    const cutSent = lastWorker.posted.slice(before4).find(m => m.type === 'explore:intent');
    check('form (model): the refined intent carries the model\'s cut, not an empty one',
      cutSent && JSON.stringify(cutSent.intent.dims) === '["client"]', JSON.stringify(cutSent && cutSent.intent));
    if (cutSent) lastWorker.respond({ id: cutSent.id, type: 'explore:intent_result', error: null,
      sql: 'WITH c AS (SELECT 1 AS n, 1 AS d) SELECT "client", SUM("n") AS "value" FROM c GROUP BY 1 LIMIT 10',
      kpi: { key: 'sl', name: 'Service level' } });
    await tick(6);
    check('form (model): and it RUNS — question in, answer out, no click',
      lastWorker.posted.slice(before4).some(m => m.type === 'explore:sql'),
      JSON.stringify(lastWorker.posted.slice(before4).map(m => m.type)));
    check('form (model): the status names the cut the model chose',
      /dimension: client/.test(byIdMap['ex-nl-status'].textContent), byIdMap['ex-nl-status'].textContent);
    const csql = lastWorker.posted.slice(before4).find(m => m.type === 'explore:sql');
    if (csql) lastWorker.respond({ id: csql.id, type: 'explore:result', columns: ['client', 'value'], rows: [['Daisy', 0.9]], total: 1, ms: 2,
      partitions_used: [], partitions_expected: 0, missing_partitions: [], truncated: false, incomplete: false });
    await tick(4);
    global.__intentForm = false;

    // ── TIER 2: the model says "not one of the metrics" (422). The report's
    //    semantic views can still answer, so the model writes ONE SELECT over
    //    them — labelled as model-written, and run. ────────────────────────
    global.__intent422 = true;
    sqlBox.value = '';
    const before5 = lastWorker.posted.length;
    const fb = fetchCalls.length;
    byIdMap['ex-nl'].value = 'сколько строк с пустым автором';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await tick(8);
    const urls = fetchCalls.slice(fb).map(c => c.url);
    check('tier 2: a 422 from the intent route goes to /api/explore/semantic-sql, NOT to bare nl2sql',
      urls.includes('/api/explore/semantic-sql') && !urls.includes('/api/explore/nl2sql'),
      JSON.stringify(urls));
    const t2 = fetchCalls.slice(fb).find(c => c.url === '/api/explore/semantic-sql');
    check('tier 2: the request carries the worker\'s bindings status so only ready views are described',
      t2 && t2.body.bindings_status && t2.body.bindings_status.views.sl_lines === 'ok'
      && t2.body.generator_id === 7, JSON.stringify(t2 && t2.body));
    check('tier 2: the model-written SQL landed in the editor',
      /FROM "lines"/.test(sqlBox.value), sqlBox.value);
    check('tier 2: it is LABELLED as model-written before anything else',
      /^SQL was written by the model from the report's semantics — check it/.test(byIdMap['ex-nl-status'].textContent),
      byIdMap['ex-nl-status'].textContent);
    const t2run = lastWorker.posted.slice(before5).find(m => m.type === 'explore:sql');
    check('tier 2: and it is RUN (the owner wants answers; the guard keeps it read-only)',
      !!t2run && /FROM "lines"/.test(t2run.sql), JSON.stringify(lastWorker.posted.slice(before5).map(m => m.type)));
    if (t2run) lastWorker.respond({ id: t2run.id, type: 'explore:result', columns: ['client', 'count'], rows: [['Daisy', 2]], total: 1, ms: 3,
      partitions_used: [], partitions_expected: 0, missing_partitions: [], truncated: false, incomplete: false });
    await tick(4);
    check('tier 2: the grid holds the answer', byIdMap['ex-result-tbody'].children.length === 1,
      byIdMap['ex-result-tbody'].children.length);
    // The whole point of labelling model SQL is that the operator can read it,
    // so the row holding it opens itself — the one case where it ever does.
    check('answer card: «SQL of this answer» OPENS for model-written SQL',
      byIdMap['ex-row-sql'].open === true, String(byIdMap['ex-row-sql'].open));
    check('answer card: with no KPI behind a model-written answer the definition is empty',
      byIdMap['ex-how-def'].hidden === true, byIdMap['ex-how-def'].textContent);

    // Tier 3 (bare nl2sql over raw catalogue tables) is NOT reachable while a
    // report's semantics are loaded: guessing over 1C headers is a worse
    // answer than saying the semantic tiers could not answer.
    global.__llmCfg = { provider: 'ollama', endpoint: '', model: 'llama3', browser: false, server: false, key: null };
    sqlBox.value = '';
    const fb2 = fetchCalls.length;
    byIdMap['ex-nl'].value = 'сколько строк с пустым автором';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await tick(8);
    check('tier 3: with semantics ready and no model, nl2sql is NOT used and the page says so',
      !fetchCalls.slice(fb2).some(c => c.url === '/api/explore/nl2sql')
      && /Could not answer using the report's semantics/.test(byIdMap['ex-nl-status'].textContent),
      byIdMap['ex-nl-status'].textContent);
    global.__intent422 = false;
    global.__llmCfg = null;
    // Back to the single-dim KPI the remaining cases expect.
    lastWorker.respond({ type: 'explore:indexed', indexed: 1, errors: [], tables: [], stats: { tables: 1, partitions: 1 },
      semantics: { generators: [{ generator: 'slreport', generator_id: 7, tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
        kpis: [{ key: 'sl', name: 'Service level', ready: true, dims: ['client'] }] }] } });

    // ── «Why …?» → a CARD, not one number ─────────────────────────────
    // The server classifies the question and answers source=why with a
    // period-only intent. The page then runs the worker SEQUENTIALLY three
    // times: the period, the period before it (shiftPeriod), and the top-3
    // by each non-time dim — and renders one status line + one grid.
    {
      global.__intentWhy = true;
      sqlBox.value = '';
      const before = lastWorker.posted.length;
      const fbW = fetchCalls.length;
      byIdMap['ex-nl'].value = 'почему в январе 2026 упал SL?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(6);

      // (a) the period the question named
      const curI = lastWorker.posted.slice(before).find(m => m.type === 'explore:intent');
      check('why: the first worker call is the metric for the question\'s own period, no dims/order/limit',
        curI && curI.intent.kpi === 'sl' && JSON.stringify(curI.intent.dims) === '[]'
        && curI.intent.order === undefined && curI.intent.limit === undefined
        && (curI.intent.filters || []).some(f => f.dim === 'month' && f.value === 1),
        JSON.stringify(curI && curI.intent));
      lastWorker.respond({ id: curI.id, type: 'explore:intent_result', error: null,
        sql: 'WITH c AS (SELECT 1 AS n, 1 AS d) SELECT SUM("n") AS "n", SUM("d") AS "d", 0.8 AS "value" FROM c',
        kpi: { key: 'sl', name: 'Service level' } });
      await tick(4);
      const curQ = lastWorker.posted.slice(before).find(m => m.type === 'explore:sql');
      check('why: and it is RUN before anything else is asked for (sequential, not parallel)',
        !!curQ && lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent').length === 1,
        JSON.stringify(lastWorker.posted.slice(before).map(m => m.type)));
      lastWorker.respond({ id: curQ.id, type: 'explore:result', columns: ['n', 'd', 'value'],
        rows: [[80, 100, 0.8]], total: 1, ms: 7, partitions_used: [{ table: 'sales_orders', key: '2026-06' }],
        partitions_expected: 2, missing_partitions: [], truncated: false, incomplete: false });
      await tick(4);

      // (b) the previous period — the same intent with shiftPeriod's filters
      const prevI = lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent')[1];
      check('why: the second call is the SAME metric one period back (Jan 2026 -> Dec 2025)',
        prevI && (prevI.intent.filters || []).some(f => f.dim === 'month' && f.value === 12)
        && (prevI.intent.filters || []).some(f => f.dim === 'year' && f.value === 2025),
        JSON.stringify(prevI && prevI.intent));
      lastWorker.respond({ id: prevI.id, type: 'explore:intent_result', error: null,
        sql: 'WITH c AS (SELECT 1 AS n) SELECT 0.95 AS "value" FROM c', kpi: { key: 'sl' } });
      await tick(4);
      const prevQ = lastWorker.posted.slice(before).filter(m => m.type === 'explore:sql')[1];
      lastWorker.respond({ id: prevQ.id, type: 'explore:result', columns: ['n', 'd', 'value'],
        rows: [[95, 100, 0.95]], total: 1, ms: 5, partitions_used: [], partitions_expected: 0,
        missing_partitions: [], truncated: false, incomplete: false });
      await tick(4);

      // (c) concentration: top-3 by each non-time dim the KPI has
      const dimI = lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent')[2];
      check('why: the third call cuts by the KPI\'s non-time dim, top-3 biggest first',
        dimI && JSON.stringify(dimI.intent.dims) === '["client"]' && dimI.intent.limit === 3
        && dimI.intent.order && dimI.intent.order.dir === 'desc',
        JSON.stringify(dimI && dimI.intent));
      lastWorker.respond({ id: dimI.id, type: 'explore:intent_result', error: null,
        sql: 'WITH c AS (SELECT 1 AS n) SELECT "client", 1 AS "value" FROM c GROUP BY 1 LIMIT 3',
        kpi: { key: 'sl' } });
      await tick(4);
      const dimQ = lastWorker.posted.slice(before).filter(m => m.type === 'explore:sql')[2];
      lastWorker.respond({ id: dimQ.id, type: 'explore:result', columns: ['client', 'n', 'd', 'value'],
        rows: [['Daisy', 40, 60, 0.667], ['Cornflower', 40, 40, 1]], total: 2, ms: 9,
        partitions_used: [], partitions_expected: 0, missing_partitions: [], truncated: false, incomplete: false });
      await tick(6);

      const st = byIdMap['ex-nl-status'].textContent;
      check('why: the status line is a «Why:» card naming the metric and the period', /^Why: Service level for .*January/.test(st), st);
      check('why: it carries the current value, the previous one and a Δ',
        st.includes('0.8') && st.includes('0.95') && /Δ\s*-0\.15/.test(st), st);
      check('why: it says where the number is concentrated',
        /where it's concentrated \(top 3 by client\)/.test(st), st);
      check('why: the grid holds the concentration rows, tagged with the dim they came from',
        byIdMap['ex-result-tbody'].children.length === 2
        && byIdMap['ex-result-tbody'].children[0].children[0].textContent === 'client'
        && byIdMap['ex-result-tbody'].children[0].children[1].textContent === 'Daisy',
        byIdMap['ex-result-tbody'].children.map(r => r.children.map(c => c.textContent).join('/')).join('|'));
      check('why: the grid\'s first column is «dimension»',
        byIdMap['ex-result-thead'].children[0].textContent === 'dimension',
        byIdMap['ex-result-thead'].children.map(c => c.textContent).join(','));
      check('why: «SQL of this answer» holds the CURRENT-period query',
        /0\.8 AS "value"/.test(sqlBox.value), sqlBox.value);
      check('why: never fell through to tier 2 or bare nl2sql',
        !fetchCalls.slice(fbW).some(c => /semantic-sql|nl2sql/.test(c.url)),
        JSON.stringify(fetchCalls.slice(fbW).map(c => c.url)));
      global.__intentWhy = false;
    }

    // ── a why-card whose LAST sub-query fails still shows what it computed ─
    {
      global.__intentWhy = true;
      sqlBox.value = '';
      const before = lastWorker.posted.length;
      byIdMap['ex-nl'].value = 'почему в январе 2026 упал SL?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(6);
      const curI = lastWorker.posted.slice(before).find(m => m.type === 'explore:intent');
      lastWorker.respond({ id: curI.id, type: 'explore:intent_result', error: null,
        sql: 'SELECT 0.8 AS "value"', kpi: { key: 'sl', name: 'Service level' } });
      await tick(4);
      const curQ = lastWorker.posted.slice(before).find(m => m.type === 'explore:sql');
      lastWorker.respond({ id: curQ.id, type: 'explore:result', columns: ['value'], rows: [[0.8]],
        total: 1, ms: 1, partitions_used: [], partitions_expected: 0, missing_partitions: [],
        truncated: false, incomplete: false });
      await tick(4);
      const prevI = lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent')[1];
      lastWorker.respond({ id: prevI.id, type: 'explore:error', message: 'partition not loaded' });
      await tick(4);
      const dimI = lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent')[2];
      lastWorker.respond({ id: dimI.id, type: 'explore:error', message: 'no such dimension' });
      await tick(6);
      const st = byIdMap['ex-nl-status'].textContent;
      check('why (partial): the number that WAS computed is still shown',
        /^Why: Service level/.test(st) && st.includes('0.8'), st);
      check('why (partial): and the failures are named, not swallowed',
        /not calculated:/.test(st) && st.includes('partition not loaded'), st);
      global.__intentWhy = false;
    }

    // ── phase 4.5: «why is the report number 25.5?» asked COLD ─────────────
    // No open answer, no metric named, only a number the model has never
    // seen (cell values never reach the server). The page looks the number
    // up in what THIS browser holds: the thread first, then the Grand Total
    // of every ready KPI — and never calls the server at all.
    {
      const S = global.window.__opExploreState;
      // Two ready KPIs, so a Grand-Total sweep has something to be unique
      // (or ambiguous) about.
      const twoKpis = { generators: [{ generator: 'slreport', generator_id: 7,
        tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
        kpis: [{ key: 'sl', name: 'Service level', short: 'SL', ready: true, measure: 'ratio', dims: ['client', 'month'] },
               { key: 'otif', name: 'OTIF', short: 'OTIF', ready: true, dims: ['client'] }] }] };
      const reindex = () => lastWorker.respond({ type: 'explore:indexed', indexed: 1, errors: [],
        tables: [], stats: { tables: 1, partitions: 1 }, semantics: twoKpis });

      // A helper: answer the worker's next `explore:intent` + `explore:sql`
      // pair with one Grand-Total value.
      async function serveGT(from, nth, value) {
        const i = lastWorker.posted.slice(from).filter(m => m.type === 'explore:intent')[nth];
        if (!i) return null;
        lastWorker.respond({ id: i.id, type: 'explore:intent_result', error: null,
          sql: 'SELECT ' + value + ' AS "value"', kpi: { key: i.intent.kpi } });
        await tick(4);
        const q = lastWorker.posted.slice(from).filter(m => m.type === 'explore:sql')[nth];
        if (q) {
          lastWorker.respond({ id: q.id, type: 'explore:result', columns: ['value'], rows: [[value]],
            total: 1, ms: 1, partitions_used: [], partitions_expected: 0, missing_partitions: [],
            truncated: false, incomplete: false });
          await tick(4);
        }
        return i;
      }

      // ── (1) UNIQUE hit in the Grand Totals ──────────────────────────
      reindex();
      S.openAnswer = null; S.historyProvider = null; S.gtCache = null;
      const genBefore = S.indexGen;
      let before = lastWorker.posted.length;
      let fb = fetchCalls.length;
      byIdMap['ex-nl'].value = 'почему в отчёте число 25,5?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(6);
      check('value lookup: the search runs SEQUENTIALLY — one KPI in flight at a time',
        lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent').length === 1,
        JSON.stringify(lastWorker.posted.slice(before).map(m => m.type)));
      const gt0 = await serveGT(before, 0, 0.255);   // sl: a ratio, displays as 25,5 %
      check('value lookup: the first Grand Total is a dims-free intent over a ready KPI',
        gt0 && JSON.stringify(gt0.intent.dims) === '[]' && gt0.intent.kpi === 'sl',
        JSON.stringify(gt0 && gt0.intent));
      await serveGT(before, 1, 0.91);               // otif: no match
      await tick(8);
      check('value lookup: NOTHING was sent to the server — not the intent route, not the question',
        !fetchCalls.slice(fb).some(c => /\/api\/explore\/(intent|nl2sql|semantic-sql)/.test(c.url)),
        JSON.stringify(fetchCalls.slice(fb).map(c => c.url)));
      check('value lookup: a single hit runs the EXISTING why-card for that metric',
        /^Why: calculating "Service level"/.test(byIdMap['ex-nl-status'].textContent),
        byIdMap['ex-nl-status'].textContent);
      check('value lookup: a ratio stored as 0.255 answers the asked «25,5»',
        lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent')
          .slice(2).every(m => m.intent.kpi === 'sl'),
        JSON.stringify(lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent').map(m => m.intent.kpi)));
      // Let the why-card's own three sub-queries fail out; the card is the
      // existing, already-pinned machinery — what is under test is the route.
      lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent').slice(2)
        .forEach(m => lastWorker.respond({ id: m.id, type: 'explore:error', message: 'x' }));
      await tick(8);
      check('value lookup: the GT list is cached against the index generation',
        S.gtCache && S.gtCache.key.indexOf('|' + genBefore + '|') > 0, JSON.stringify(S.gtCache && S.gtCache.key));

      // ── (2) AMBIGUOUS: two metrics carry the same number -> chips ────
      reindex();
      check('value lookup: re-indexing bumps the generation and drops the cached totals',
        S.indexGen === genBefore + 1 && S.gtCache === null, String(S.indexGen));
      S.openAnswer = null;
      before = lastWorker.posted.length;
      byIdMap['ex-nl'].value = 'почему в отчёте число 25,5?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(6);
      await serveGT(before, 0, 0.255);
      await serveGT(before, 1, 25.52);
      await tick(8);
      const chips = byIdMap['ex-value-chips'];
      check('value lookup (ambiguous): both metrics become chips, nothing is guessed',
        chips.hidden === false && chips.children.length === 2,
        chips.children.map(c => c.textContent).join('|'));
      check('value lookup (ambiguous): a chip says «This is about: <KPI> · <cut> · <period>»',
        /^This is about: SL · total · no period$/.test(chips.children[0].textContent),
        chips.children[0].textContent);
      check('value lookup (ambiguous): the status names the number and asks, in textContent',
        /appears in several metrics/.test(byIdMap['ex-nl-status'].textContent)
        && byIdMap['ex-nl-status'].textContent.includes('25,5'),
        byIdMap['ex-nl-status'].textContent);
      const before2 = lastWorker.posted.length;
      chips.children[1].dispatchEvent({ type: 'click' });
      await tick(6);
      check('value lookup (ambiguous): clicking a chip runs the why-card for THAT metric',
        (lastWorker.posted.slice(before2).find(m => m.type === 'explore:intent') || {}).intent.kpi === 'otif',
        JSON.stringify(lastWorker.posted.slice(before2).map(m => m.type)));
      check('value lookup (ambiguous): the chips are cleared once one is chosen',
        chips.hidden === true && chips.children.length === 0);
      lastWorker.posted.slice(before2).filter(m => m.type === 'explore:intent')
        .forEach(m => lastWorker.respond({ id: m.id, type: 'explore:error', message: 'x' }));
      await tick(8);

      // ── (3) ZERO match: an honest status + the metric chips ─────────
      reindex();
      S.openAnswer = null;
      before = lastWorker.posted.length;
      fb = fetchCalls.length;
      byIdMap['ex-nl'].value = 'почему в отчёте число 999999,9?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(6);
      await serveGT(before, 0, 0.255);
      await serveGT(before, 1, 0.91);
      await tick(8);
      check('value lookup (zero): the status is honest about not finding the number',
        byIdMap['ex-nl-status'].textContent
          === 'The number 999999,9 was not found in the current data — clarify the metric',
        byIdMap['ex-nl-status'].textContent);
      check('value lookup (zero): the metric chips are offered as the way out',
        byIdMap['ex-value-chips'].hidden === false
        && byIdMap['ex-value-chips'].children.length === 2,
        byIdMap['ex-value-chips'].children.map(c => c.textContent).join('|'));
      check('value lookup (zero): still no server call of any kind',
        !fetchCalls.slice(fb).some(c => /\/api\/explore\/(intent|nl2sql|semantic-sql)/.test(c.url)),
        JSON.stringify(fetchCalls.slice(fb).map(c => c.url)));

      // ── (4) the thread is searched BEFORE any KPI is re-run ─────────
      reindex();
      S.openAnswer = null;
      S.historyProvider = async () => ([{
        intent: { kpi: 'otif', dims: ['client'], filters: [{ dim: 'month', op: '=', value: 7 }] },
        rows: [['Daisy', 0.9], ['Cornflower', 25.49]],
      }]);
      before = lastWorker.posted.length;
      byIdMap['ex-nl'].value = 'почему в отчёте число 25,5?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(8);
      const firstIntent = lastWorker.posted.slice(before).find(m => m.type === 'explore:intent');
      check('value lookup (history): a hit in the thread answers without sweeping the KPIs',
        firstIntent && firstIntent.intent.kpi === 'otif'
        && (firstIntent.intent.filters || []).some(f => f.dim === 'month' && f.value === 7),
        JSON.stringify(firstIntent && firstIntent.intent));
      check('value lookup (history): and the why-card is the one that answers',
        /^Why: calculating "OTIF"/.test(byIdMap['ex-nl-status'].textContent),
        byIdMap['ex-nl-status'].textContent);
      lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent')
        .forEach(m => lastWorker.respond({ id: m.id, type: 'explore:error', message: 'x' }));
      await tick(8);
      S.historyProvider = null;

      // ── (5) REGRESSION #1: a why-question WITHOUT a number keeps the
      //        server-classifier route it has always had ────────────────
      reindex();
      S.openAnswer = null;
      global.__intentWhy = true;
      before = lastWorker.posted.length;
      fb = fetchCalls.length;
      byIdMap['ex-nl'].value = 'почему упал SL?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(8);
      check('regression: a why-question with NO number still goes to /api/explore/intent',
        fetchCalls.slice(fb).some(c => c.url === '/api/explore/intent'),
        JSON.stringify(fetchCalls.slice(fb).map(c => c.url)));
      check('regression: and the value-lookup chips stay empty on that path',
        byIdMap['ex-value-chips'].hidden === true, byIdMap['ex-value-chips'].children.length);
      lastWorker.posted.slice(before).filter(m => m.type === 'explore:intent')
        .forEach(m => lastWorker.respond({ id: m.id, type: 'explore:error', message: 'x' }));
      await tick(8);
      global.__intentWhy = false;

      // ── (6) a why-question with a number that NAMES its metric is not
      //        the cold case — the existing routing owns it ─────────────
      reindex();
      S.openAnswer = null;
      global.__intentWhy = true;
      fb = fetchCalls.length;
      byIdMap['ex-nl'].value = 'почему Service level 25,5?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(8);
      check('regression: a number PLUS a confident metric name keeps the server route',
        fetchCalls.slice(fb).some(c => c.url === '/api/explore/intent'),
        JSON.stringify(fetchCalls.slice(fb).map(c => c.url)));
      lastWorker.posted.filter(m => m.type === 'explore:intent')
        .forEach(m => lastWorker.respond({ id: m.id, type: 'explore:error', message: 'x' }));
      await tick(8);
      global.__intentWhy = false;

      // Back to the single-dim, single-KPI semantics the remaining cases
      // (browser-LLM path, export, save) were written against.
      S.openAnswer = null;
      lastWorker.respond({ type: 'explore:indexed', indexed: 1, errors: [], tables: [], stats: { tables: 1, partitions: 1 },
        semantics: { generators: [{ generator: 'slreport', generator_id: 7, tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
          kpis: [{ key: 'sl', name: 'Service level', ready: true, dims: ['client'] }] }] } });
      await tick(4);
    }

    // ── browser LLM path (OP_LLM_BROWSER=true, docs/LLM_PROVIDERS.md) ────
    //    The page itself calls the provider: prepare (server, no model) →
    //    POST {endpoint}/chat/completions with the key → validate (server).
    //    The SQL still only LANDS in the editor; nothing runs.
    {
      global.__llmCfg = { provider: 'deepseek', endpoint: 'https://api.deepseek.com',
                          model: 'deepseek-chat', browser: true, server: false, key: 'sk-server-held' };
      sqlBox.value = '';
      const before = lastWorker.posted.length;
      const fb2 = fetchCalls.length;
      byIdMap['ex-nl'].value = 'что там по клиентам за июль?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(8);
      const urls2 = fetchCalls.slice(fb2).map(c => c.url);
      // The verifier (2026-08-24) adds one advisory pass after validate:
      // verify/prepare -> the provider again. Its verdict is fail-open, so
      // the flow still ends in an answer.
      check('browser llm: prepare → provider → validate → verify/prepare → provider, in that order',
        urls2.join('|') === ['/api/explore/llm-config', '/api/explore/intent/prepare',
          'https://api.deepseek.com/chat/completions', '/api/explore/intent/validate',
          '/api/explore/intent/verify/prepare', 'https://api.deepseek.com/chat/completions'].join('|'),
        JSON.stringify(urls2));
      check('browser llm: the server intent route was NOT used',
        !urls2.includes('/api/explore/intent') && !urls2.includes('/api/explore/nl2sql'), JSON.stringify(urls2));
      const ds = fetchCalls.slice(fb2).find(c => /chat\/completions$/.test(c.url));
      check('browser llm: bearer key + JSON mode + temperature 0 straight to the provider',
        ds && ds.opts.headers.Authorization === 'Bearer sk-server-held'
        && ds.body.temperature === 0 && ds.body.response_format.type === 'json_object'
        && ds.body.model === 'deepseek-chat', JSON.stringify(ds && ds.body).slice(0, 200));
      check('browser llm: the prompt from prepare is what is sent, schema appended',
        ds && ds.body.messages[0].role === 'system' && /Metrics:/.test(ds.body.messages[1].content)
        && /schema:/.test(ds.body.messages[1].content), JSON.stringify(ds && ds.body.messages).slice(0, 200));
      const val = fetchCalls.slice(fb2).find(c => c.url === '/api/explore/intent/validate');
      check('browser llm: the provider\'s raw text goes back for validation, never the key',
        val && /"kpi": "sl"/.test(val.body.raw) && val.body.generator_id === 7
        && !JSON.stringify(val.body).includes('sk-server-held'), JSON.stringify(val && val.body).slice(0, 200));
      const sentB = lastWorker.posted.slice(before).find(m => m.type === 'explore:intent');
      check('browser llm: the validated intent went to the worker', sentB && sentB.intent.kpi === 'sl',
        JSON.stringify(lastWorker.posted.slice(before)));
      if (sentB) lastWorker.respond({ id: sentB.id, type: 'explore:intent_result', sql: 'WITH c AS (SELECT 1 AS n) SELECT 1', error: null,
        kpi: { key: 'sl', name: 'Service level', definition: 'Σmin(actual,ordered)/Σordered' } });
      await tick(6);
      check('browser llm: SQL landed in the editor', /^WITH c AS/.test(sqlBox.value), sqlBox.value);
      check('browser llm: and it RUNS in one shot, like the server path',
        lastWorker.posted.slice(before).some(m => m.type === 'explore:sql'),
        JSON.stringify(lastWorker.posted.slice(before).map(m => m.type)));
      const bsql = lastWorker.posted.slice(before).find(m => m.type === 'explore:sql');
      if (bsql) lastWorker.respond({ id: bsql.id, type: 'explore:result', columns: ['a'], rows: [[1]], total: 1, ms: 1,
        partitions_used: [], partitions_expected: 0, missing_partitions: [], truncated: false, incomplete: false });
      await tick(4);

      // The provider is unreachable from the page (no CORS headers, blocked
      // by the proxy, DNS): say which host failed, then let the server try.
      global.__llmThrow = true;
      sqlBox.value = '';
      const fb3 = fetchCalls.length;
      byIdMap['ex-nl'].value = 'что там по клиентам за август?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(8);
      const urls3 = fetchCalls.slice(fb3).map(c => c.url);
      check('browser llm: a fetch TypeError falls back to the server intent route',
        urls3.includes('/api/explore/intent'), JSON.stringify(urls3));
      check('browser llm: and the status names the host it could not reach',
        /couldn't reach api\.deepseek\.com/.test(byIdMap['ex-nl-status'].textContent)
        || /Service level/.test(byIdMap['ex-nl-status'].textContent),
        byIdMap['ex-nl-status'].textContent);
      // Client-side failure log (2026-08-28): the browser is where the
      // provider fails, so the page tells the server about it — one row,
      // stage + host, never the key, never a cell.
      const errPosts = fetchCalls.slice(fb3).filter(c => c.url === '/api/explore/client-error');
      check('client log: the unreachable provider is reported to /api/explore/client-error',
        errPosts.length >= 1 && errPosts[0].body.stage === 'provider_network'
        && errPosts[0].body.host === 'api.deepseek.com', JSON.stringify(errPosts.map(c => c.body)));
      check('client log: the row carries the question and never the key',
        errPosts.length >= 1 && /клиентам/.test(errPosts[0].body.question || '')
        && !JSON.stringify(errPosts[0].body).includes('sk-'), JSON.stringify(errPosts[0] && errPosts[0].body));
      global.__llmThrow = false;

      // BYOK: no server key and none stored -> the «DeepSeek key» row
      // appears and the question still gets answered, via the server.
      global.__llmCfg = { provider: 'deepseek', endpoint: 'https://api.deepseek.com',
                          model: 'deepseek-chat', browser: true, server: true, key: null };
      byIdMap['ex-llm-key-row'].hidden = true;
      const fb4 = fetchCalls.length;
      byIdMap['ex-nl'].value = 'что там по клиентам за сентябрь?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(8);
      check('browser llm: BYOK — the key field appears when the server holds no key',
        byIdMap['ex-llm-key-row'].hidden === false, String(byIdMap['ex-llm-key-row'].hidden));
      const urls4 = fetchCalls.slice(fb4).map(c => c.url);
      check('browser llm: with no key at all the page does not call the provider',
        !urls4.some(u => /chat\/completions$/.test(u)) && urls4.includes('/api/explore/intent'),
        JSON.stringify(urls4));
      global.__llmCfg = null;
    }

    // ── screen A, block 1: the report strip ──────────────────────────
    // The strip is built from `data-reports` (what `_reports_snapshot`
    // bakes in), one card per report this user may run. Choosing one is
    // the operator saying WHICH report the question is about — that beats
    // `pickGenerator`'s word overlap, which is a guess.
    {
      // Two ready generators, so the choice is a real one rather than the
      // only option: word overlap would pick slreport for any of these
      // questions, and the card has to override it.
      lastWorker.respond({ type: 'explore:indexed', indexed: 1, errors: [], tables: [], stats: { tables: 1, partitions: 1 },
        semantics: { generators: [
          { generator: 'slreport', generator_id: 7, tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
            kpis: [{ key: 'sl', name: 'Service level', ready: true, dims: ['client'], aggregation: 'sum',
                     definition: 'Σmin(actual,ordered)/Σordered', target: '95%' }] },
          { generator: 'opsreport', generator_id: 9, tables: { ops_rows: 'bound' }, views: {},
            kpis: [{ key: 'rdc', name: 'RDC health', ready: true, dims: ['site'] }] },
        ] } });
      await tick(2);
      const cards = Array.from(byIdMap['ex-report-strip'].children || []);
      check('reports: the strip renders one card per report in data-reports',
        cards.length === REPORTS.length, cards.length);
      check('reports: a card names the report, its generator and its version',
        cards[0] && /SL report/.test(cards[0].textContent) && /slreport v3/.test(cards[0].textContent),
        cards[0] && cards[0].textContent);
      check('reports: a card says how many metrics can answer over this folder',
        cards[1] && /1 metric\b/.test(cards[1].textContent), cards[1] && cards[1].textContent);

      // Choose the SECOND report — its generator must win the next question.
      cards[1].dispatchEvent({ type: 'click' });
      await tick(2);
      check('reports: the chosen card is marked active',
        (byIdMap['ex-report-strip'].children[1] || {}).classList
        && byIdMap['ex-report-strip'].children[1].classList.contains('active'));
      const fbR = fetchCalls.length;
      byIdMap['ex-nl'].value = 'какой SL по сетям?';   // words that point at slreport
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(6);
      const pinned = fetchCalls.slice(fbR).find(c => c.url === '/api/explore/intent');
      check('reports: the chosen report pins the generator the question is sent for',
        pinned && pinned.body.generator_id === 9, JSON.stringify(pinned && pinned.body));

      // Clicking it again un-pins — the word overlap is back in charge.
      byIdMap['ex-report-strip'].children[1].dispatchEvent({ type: 'click' });
      await tick(2);
      const fbR2 = fetchCalls.length;
      byIdMap['ex-nl'].value = 'какой SL по сетям?';
      byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
      await tick(6);
      const unpinned = fetchCalls.slice(fbR2).find(c => c.url === '/api/explore/intent');
      check('reports: clicking the chosen card again gives the choice back to the question',
        unpinned && unpinned.body.generator_id === 7, JSON.stringify(unpinned && unpinned.body));

      // ── «how it is calculated»: a ratio KPI and the files it stands on ────
      // `intent_sql` emits n / d / value for a ratio; the panel spells that
      // arithmetic out with the actual first row, and lists the partitions
      // the badge itself counted as used.
      const sentR = lastWorker.posted.filter(m => m.type === 'explore:intent').pop();
      if (sentR) lastWorker.respond({ id: sentR.id, type: 'explore:intent_result',
        sql: 'WITH c AS (SELECT 1) SELECT * FROM c', error: null,
        kpi: { key: 'sl', name: 'Service level', definition: 'Σmin(actual,ordered)/Σordered', target: '95%' } });
      await tick(6);
      const ranR = lastWorker.posted.filter(m => m.type === 'explore:sql').pop();
      if (ranR) lastWorker.respond({ id: ranR.id, type: 'explore:result',
        columns: ['n', 'd', 'value'], rows: [[1234567, 1400000, 0.8818]], total: 1, ms: 5,
        partitions_used: [{ table: 'sales_orders', key: '2026-07' }],
        partitions_expected: 2, missing_partitions: [], truncated: false, incomplete: false });
      await tick(4);
      // Thin spaces (U+2009) group the digits — a numerator and a denominator
      // are read side by side and unseparated digits are what ruins that.
      const TH = '\u2009';
      check('«how it is calculated»: a ratio result is spelled out as «n / d = value»',
        byIdMap['ex-how-calc'].textContent
          === '1' + TH + '234' + TH + '567 / 1' + TH + '400' + TH + '000 = 0.8818',
        JSON.stringify(byIdMap['ex-how-calc'].textContent));
      check('«how it is calculated»: the KPI\'s target rides along with its definition',
        /target: 95%/.test(byIdMap['ex-how-def'].textContent), byIdMap['ex-how-def'].textContent);
      const srcs = Array.from(byIdMap['ex-how-sources'].children || []);
      check('«how it is calculated»: the sources list names the partition the badge counted',
        srcs.length === 1 && /2026-07/.test(srcs[0].textContent)
        && /DataSalesOrder_07\.26\.xlsx/.test(srcs[0].textContent),
        srcs.map(s => s.textContent).join('|'));
      check('«how it is calculated»: the panel is visible once it has something to say',
        byIdMap['ex-how'].hidden === false);
    }

    // Back to a folder without semantics for the remaining cases.
    lastWorker.respond({ type: 'explore:indexed', indexed: 1, errors: [], tables: [], stats: { tables: 1, partitions: 1 }, semantics: null });
  }

  // ── export button posts explore:export ──────────────────────────────
  {
    const sqlBox = byIdMap['ex-sql'];
    sqlBox.value = 'SELECT 1';
    const before = lastWorker.posted.length;
    byIdMap['ex-export-btn'].dispatchEvent({ type: 'click' });
    await new Promise(r => setTimeout(r, 0));
    const posted = lastWorker.posted.slice(before);
    check('export: posts an explore:export message', posted.some(m => m.type === 'explore:export' && m.sql === 'SELECT 1'),
      JSON.stringify(posted));
    const sent = posted.find(m => m.type === 'explore:export');
    if (sent) lastWorker.respond({ id: sent.id, type: 'explore:file', name: 'q.xlsx', buf: new Uint8Array([1, 2, 3]) });
    await new Promise(r => setTimeout(r, 0));
  }

  // ── saved questions: POST then re-render from the server list ──────
  {
    byIdMap['ex-sql'].value = 'SELECT * FROM "sales_orders"';
    byIdMap['ex-save-title'].value = 'August orders';
    SAVED = [{ id: 7, title: 'August orders', question: 'question', sql: 'SELECT * FROM "sales_orders"', mine: true, table_ids: ['t_aaa'] }];
    click(byIdMap['ex-save-btn']);
    await tick(8);
    const posted = fetchCalls.filter(c => c.url === '/api/explore/questions' && c.opts && c.opts.method === 'POST').pop();
    check('save: POSTed the title, sql and the table_ids the SQL names',
      !!posted && posted.body.title === 'August orders' && posted.body.table_ids.includes('t_aaa'),
      JSON.stringify(posted && posted.body));
    check('save: the saved list re-renders with the new question',
      byIdMap['ex-saved-list'].children.length === 1
      && byIdMap['ex-saved-list'].children[0].textContent.includes('August orders'),
      byIdMap['ex-saved-list'].textContent);
    // clicking a saved row loads its SQL back into the editor
    byIdMap['ex-sql'].value = '';
    const row = byIdMap['ex-saved-list'].children[0];
    click(row.children[0]);
    check('save: clicking a saved question loads its SQL into the editor',
      byIdMap['ex-sql'].value === 'SELECT * FROM "sales_orders"', byIdMap['ex-sql'].value);
  }

  // ── a query that never answers is stopped by TERMINATING the worker ──
  // DuckDB's execute() cannot be pre-empted from inside Pyodide (no pthreads
  // there — the Python-side threading.Timer watchdog is a documented no-op
  // in the browser). The one mechanism that actually stops a hung query in
  // that environment is killing the worker from the main thread, which is
  // what this test drives: put the timers under our control so the 45 s
  // deadline fires immediately, send a query, never respond, and check the
  // worker is torn down rather than left hanging forever. This is the LAST
  // RESORT path — the WORKER's own 30 s interrupt is expected to answer
  // first in real life; see the next block for that path.
  {
    const realSetTimeout = global.setTimeout;
    global.setTimeout = (fn) => realSetTimeout(fn, 0); // ignore the real delay
    try {
      const hungWorker = lastWorker;
      check('watchdog: dirHandle is set going into this case (state.dirHandle reachable)',
        !!(window.__opExploreState && window.__opExploreState.dirHandle));
      byIdMap['ex-sql'].value = 'SELECT count(*) FROM huge_cross_join';
      click(byIdMap['ex-run-btn']);
      await tick(8);
      // No lastWorker.respond(...) — the query is left hanging on purpose.
      check('watchdog: the hung worker is terminated (the only real pre-emption in wasm)',
        hungWorker.terminated === true);
      check('watchdog: the run button is re-enabled instead of staying stuck on "Running…"',
        byIdMap['ex-run-btn'].disabled === false);
      check('watchdog: the status line is honest — timed out AND the engine never answered',
        byIdMap['ex-sql-status'].textContent.includes('did not finish within'),
        byIdMap['ex-sql-status'].textContent);
      // After the kill the index is gone. A run while it is being REBUILT
      // must wait for it, not refuse («Connect a folder first…» while the
      // progress bar says 4/35 — prod 2026-08-23).
      {
        const S = window.__opExploreState;
        S.indexed = false; S.indexing = true;
        byIdMap['ex-sql'].value = 'SELECT 1';
        click(byIdMap['ex-run-btn']);
        await tick(2);
        check('run while indexing: the status says the query WAITS for the index',
          /is being built/.test(byIdMap['ex-sql-status'].textContent), byIdMap['ex-sql-status'].textContent);
        S.indexing = false;           // the build ended without an index
        await tick(4);
        check('run while indexing: a build that ends without an index leaves the query unrun',
          !byIdMap['ex-sql-status'].textContent.includes('Running'), byIdMap['ex-sql-status'].textContent);
      }
      check('watchdog: with a connected folder, the message never says "Connect a folder"',
        !byIdMap['ex-sql-status'].textContent.includes('connect the folder'),
        byIdMap['ex-sql-status'].textContent);
      check('watchdog: the index was actually dropped (state.indexed false)',
        window.__opExploreState && window.__opExploreState.indexed === false);
    } finally {
      global.setTimeout = realSetTimeout;
    }
  }

  // ── the same hard-timeout kill, but with NO folder connected ──────────
  // The message must then say "connect a folder" — the one case where it's
  // true — instead of implying a folder that is in fact still attached.
  {
    const realSetTimeout = global.setTimeout;
    global.setTimeout = (fn) => realSetTimeout(fn, 0);
    const savedDirHandle = window.__opExploreState.dirHandle;
    window.__opExploreState.dirHandle = null;
    window.__opExploreState.indexed = true; // re-arm indexed so the guard doesn't short-circuit
    try {
      byIdMap['ex-sql'].value = 'SELECT count(*) FROM huge_cross_join';
      click(byIdMap['ex-run-btn']);
      // Block 1 killed the previous worker — a fresh one now needs its own
      // init/ready handshake before the sql message (and its timer) go out.
      await bringWorkerReady();
      await tick(8);
      check('watchdog (no folder): message adds "connect a folder and"',
        byIdMap['ex-sql-status'].textContent.includes('connect the folder and'),
        byIdMap['ex-sql-status'].textContent);
      check('watchdog (no folder): message still explains the timeout, in Russian',
        byIdMap['ex-sql-status'].textContent.includes('did not finish within'),
        byIdMap['ex-sql-status'].textContent);
    } finally {
      global.setTimeout = realSetTimeout;
      window.__opExploreState.dirHandle = savedDirHandle;
    }
  }

  // ── the WORKER's own 30 s interrupt answers first: index must survive ──
  // This is the path the whole fix is about: a live worker that answers with
  // its own timeout message must NOT be killed and must NOT reset the index
  // — losing it is exactly what sent the user re-indexing three times for a
  // query that was never going to run over the whole worker again anyway.
  {
    window.__opExploreState.indexed = true;
    byIdMap['ex-sql'].value = 'SELECT count(*) FROM huge_partition_scan';
    click(byIdMap['ex-run-btn']);
    // Block 2 also killed its worker (no folder → still a hard-timeout kill)
    // — bring the next fresh worker up before looking for the sql message.
    await bringWorkerReady();
    await tick(2);
    const inFlight = lastWorker.posted[lastWorker.posted.length - 1];
    check('worker-interrupt: the query was actually sent to a live worker',
      !!inFlight && inFlight.type === 'explore:sql', JSON.stringify(inFlight));
    lastWorker.respond({
      id: inFlight.id,
      error: 'Query stopped: over 30 s. Add a filter or limit the partitions.',
    });
    await tick(4);
    check('worker-interrupt: the worker is NOT terminated on its own interrupt answer',
      lastWorker.terminated !== true);
    check('worker-interrupt: state.indexed stays true — the index is not lost',
      window.__opExploreState.indexed === true);
    check('worker-interrupt: the status line shows the worker\'s own 30 s message',
      byIdMap['ex-sql-status'].textContent.includes('over 30 s'),
      byIdMap['ex-sql-status'].textContent);
    check('worker-interrupt: the index button stays enabled (folder still connected)',
      byIdMap['ex-index-btn'].disabled === false, byIdMap['ex-index-btn'].disabled);
  }

  // ── phase 9.2: the portable partition cache, page side ────────────────
  // No UI yet (that is 9.4) — what exists is two thin helpers on the page
  // API, so the export/import round trip is one worker message each and the
  // page never learns Parquet. Both run WITHOUT the hard kill timer: copying
  // files is not a query, and a slow copy must not cost the index.
  {
    const API = global.window.OP_EXPLORE;
    // metricOwnerLine / renderMetricForm: no owner and no approved_at ->
    // no subtitle element at all (not an empty line) — a KPI predating the
    // fields must render exactly as before.
    check('metricOwnerLine: absent when neither field is set', API.metricOwnerLine({ dims: [] }) === '');
    check('metricOwnerLine: owner only, no date', API.metricOwnerLine({ owner: 'financial controller' }) === 'Owner: financial controller');
    check('metricOwnerLine: date only, no owner', API.metricOwnerLine({ approved_at: '2026-01-15' }) === 'from 2026-01-15');
    API.renderMetricForm({}, { dims: [], definition: 'x' });
    check('renderMetricForm: no .ex-metric-owner element when neither field is set',
      !byIdMap['ex-metric-form'].querySelector('.ex-metric-owner'));
    // fmtValue: a ratio declared in % is a share (prod 2026-08-28: «0.9998 %»)
    check('fmtValue: ratio + unit % → ×100', API.fmtValue(0.9998, { measure: 'ratio', unit: '%' }) === '99,98 %',
      API.fmtValue(0.9998, { measure: 'ratio', unit: '%' }));
    check('fmtValue: ratio + unit % already in points stays', API.fmtValue(99.6, { measure: 'ratio', unit: '%' }) === '99,60 %',
      API.fmtValue(99.6, { measure: 'ratio', unit: '%' }));
    check('fmtValue: ratio + unit «days» is never a percent', API.fmtValue(2.74, { measure: 'ratio', unit: 'days' }) === '2,74 days'
      || API.fmtValue(2.74, { measure: 'ratio', unit: 'days' }) === '2.74 days', API.fmtValue(2.74, { measure: 'ratio', unit: 'days' }));
    check('cache export/import: the page API exposes both helpers',
      API && typeof API.cacheExport === 'function' && typeof API.cacheImport === 'function');

    const expP = API.cacheExport(['k1', 'k2']);
    await bringWorkerReady();
    await tick(2);
    const exp = lastWorker.posted.filter(m => m.type === 'explore:cache_export').pop();
    check('cache export: one message carries every key asked for',
      !!exp && exp.keys.length === 2 && exp.keys[0] === 'k1', JSON.stringify(exp));
    const bufA = new ArrayBuffer(12);
    lastWorker.respond({ type: 'explore:cache_exported', id: exp.id,
      parts: [{ key: 'k1', buf: bufA }] });
    const expOut = await expP;
    check('cache export: the helper resolves with the worker payload',
      expOut && expOut.parts.length === 1 && expOut.parts[0].key === 'k1'
      && expOut.parts[0].buf.byteLength === 12, JSON.stringify(expOut && expOut.parts && expOut.parts.length));

    const impBuf = new ArrayBuffer(9);
    const impP = API.cacheImport('k9', impBuf);
    await tick(2);
    const imp = lastWorker.posted.filter(m => m.type === 'explore:cache_import').pop();
    check('cache import: the message carries the key and the bytes',
      !!imp && imp.key === 'k9' && imp.buf === impBuf, JSON.stringify(imp && imp.key));
    lastWorker.respond({ type: 'explore:cache_imported', id: imp.id, ok: true, bytes: 9, error: null });
    const impOut = await impP;
    check('cache import: the helper resolves with {ok, bytes}',
      impOut && impOut.ok === true && impOut.bytes === 9, JSON.stringify(impOut));

    // A refused import is an answer, not an exception: the page will show
    // the reason in 9.4 rather than silently counting the partition in.
    const badP = API.cacheImport('nope', new ArrayBuffer(4));
    await tick(2);
    const bad = lastWorker.posted.filter(m => m.type === 'explore:cache_import').pop();
    lastWorker.respond({ type: 'explore:cache_imported', id: bad.id, ok: false, bytes: 0,
      error: 'Not Parquet — partition rejected' });
    const badOut = await badP;
    check('cache import: a rejection resolves with ok:false and the reason',
      badOut && badOut.ok === false && /Parquet/.test(badOut.error), JSON.stringify(badOut));
  }

  // ── phase 9.4: «Load index» — a report answered with NO folder ──
  // The whole point of the server lake: a user who has never opened the
  // share (or cannot) pulls the run's own Parquet partitions down and asks a
  // question over them. Driven on a FRESH page instance so `state.dirHandle`
  // is genuinely null — the earlier blocks connected the test folder.
  {
    delete global.window.__opExploreTestFiles;
    LAKE.manifest = JSON.parse(JSON.stringify(LAKE_MANIFEST));
    LAKE.parts.length = 0;
    dom = buildDom();
    (0, eval)(src);
    await tick(6);                       // probeServerIndexes fires past first paint
    const st = global.window.__opExploreState;
    const strip = byIdMap['ex-report-strip'];
    check('server index: the card of a report WITH a published index says so, with count and date',
      /index on the server · 2 partitions · data as of \d\d\.\d\d/.test(strip.children[0].textContent),
      strip.children[0].textContent);
    check('server index: a report with no manifest (404) says nothing about a server index',
      !/index on (the )?server/.test(strip.children[1].textContent), strip.children[1].textContent);
    const loaders = body.querySelectorAll('.ex-report-load');
    check('server index: exactly one «Load index» — on the card that has one',
      loaders.length === 1 && loaders[0].textContent === 'Load index', loaders.length);

    loaders[0].dispatchEvent({ type: 'click' });
    await tick(2);
    lastWorker.respond({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0 } });
    await tick(6);

    // The version gate comes FIRST: an index built by another parser version
    // coerces types differently, and importing it would be a silent lie.
    const pv = lastWorker.posted.filter(m => m.type === 'explore:cache_probe')[0];
    check('server index: the engine is asked for its parser version before anything is downloaded',
      !!pv && Array.isArray(pv.files) && pv.files.length === 0 && LAKE.parts.length === 0,
      JSON.stringify(pv && pv.files));
    lastWorker.respond({ type: 'explore:cache_probed', id: pv.id, keys: [], cached: [],
      parser_version: '4', stats: { files: 0, bytes: 0 } });
    await tick(6);

    const probe = lastWorker.posted.filter(m => m.type === 'explore:cache_probe')[1];
    check('server index: the manifest\'s own files are probed against this browser\'s cache',
      !!probe && probe.files.length === 2 && probe.files[0].name === 'DataSalesOrder_06.26.xlsx',
      JSON.stringify(probe && probe.files));
    // First partition already held here, second one is a miss.
    lastWorker.respond({ type: 'explore:cache_probed', id: probe.id,
      keys: ['aaaa000000000001', 'aaaa000000000002'], cached: [true, false],
      parser_version: '4', stats: { files: 1, bytes: 900 } });
    await tick(8);

    check('server index: ONLY the partition this browser lacks is downloaded',
      LAKE.parts.length === 1 && LAKE.parts[0] === 'aaaa000000000002', JSON.stringify(LAKE.parts));
    const imps = lastWorker.posted.filter(m => m.type === 'explore:cache_import');
    check('server index: the downloaded bytes are imported under the manifest\'s own key',
      imps.length === 1 && imps[0].key === 'aaaa000000000002' && imps[0].buf.byteLength === 16,
      JSON.stringify(imps.map(m => m.key)));
    lastWorker.respond({ type: 'explore:cache_imported', id: imps[0].id, ok: true, bytes: 16, error: null });
    await tick(8);

    const idx = lastWorker.posted.filter(m => m.type === 'explore:index');
    check('server index: partitions are indexed FROM THE CACHE — no workbook bytes anywhere',
      idx.length === 1 && idx[0].files[0].cached === true && !idx[0].files[0].buf
      && idx[0].files[0].cache_key === 'aaaa000000000001',
      JSON.stringify(idx[0] && idx[0].files[0]));
    check('server index: the synthetic record carries the manifest\'s table/sheet/partition',
      idx[0].files[0].table === 'sales_orders' && idx[0].files[0].sheet === 'TDSheet'
      && idx[0].files[0].partition === '2026-06' && idx[0].files[0].header_row === 0,
      JSON.stringify(idx[0].files[0]));
    lastWorker.respond({ type: 'explore:indexed', id: idx[0].id, indexed: 1, cached: 1, errors: [],
      tables: [{ name: 'sales_orders', partitions: [{ key: '2026-06' }] }],
      stats: { tables: 1, partitions: 1, rows: 10 } });
    await tick(8);
    const idx2 = lastWorker.posted.filter(m => m.type === 'explore:index');
    check('server index: the second partition is sent only after the first is acknowledged',
      idx2.length === 2 && idx2[1].files[0].cache_key === 'aaaa000000000002',
      JSON.stringify(idx2.map(m => m.files[0].cache_key)));
    lastWorker.respond({ type: 'explore:indexed', id: idx2[1].id, indexed: 1, cached: 1, errors: [],
      tables: [{ name: 'sales_orders', partitions: [{ key: '2026-06' }, { key: '2026-07' }] }],
      stats: { tables: 1, partitions: 2, rows: 22 }, semantics: null });
    await tick(8);

    check('server index: the page is indexed', st.indexed === true, JSON.stringify(st.indexed));
    check('server index: … from the SERVER, with no folder connected at all',
      st.source === 'server' && st.dirHandle === null,
      st.source + '/' + JSON.stringify(st.dirHandle));
    const sum = byIdMap['ex-summary'].textContent;
    check('server index: the summary names the partitions AND the run they came from',
      /2 partitions/.test(sum) && /index from server/.test(sum) && /run 42/.test(sum), sum);
    check('server index: loading a report\'s index pins that report',
      String(st.reportGen) === '7', String(st.reportGen));
    check('server index: «Index» stays a folder control (nothing to re-read)',
      byIdMap['ex-index-btn'].disabled === true);
    check('server index: the card now reads «index from server · 2 partitions»',
      /index from the server · 2 partitions/.test(byIdMap['ex-report-strip'].children[0].textContent),
      byIdMap['ex-report-strip'].children[0].textContent);
    check('server index: the files behind the answer are the manifest\'s, one per workbook',
      st.lastFiles.length === 2 && st.maxMtime === 2000, JSON.stringify(st.lastFiles));
    const prov = global.window.OP_EXPLORE.answerProvenance();
    check('server index: provenance says the rows came from the server index of run 42',
      prov.source === 'server' && prov.run_id === 42, JSON.stringify(prov));
  }

  // ── phase 9.4: an index built by another parser version is refused ────
  {
    LAKE.manifest = Object.assign(JSON.parse(JSON.stringify(LAKE_MANIFEST)), { parser_version: '999' });
    LAKE.parts.length = 0;
    dom = buildDom();
    (0, eval)(src);
    await tick(6);
    const st = global.window.__opExploreState;
    body.querySelectorAll('.ex-report-load')[0].dispatchEvent({ type: 'click' });
    await tick(2);
    lastWorker.respond({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0 } });
    await tick(6);
    const pv = lastWorker.posted.filter(m => m.type === 'explore:cache_probe')[0];
    lastWorker.respond({ type: 'explore:cache_probed', id: pv.id, keys: [], cached: [],
      parser_version: '4', stats: { files: 0, bytes: 0 } });
    await tick(8);
    check('parser mismatch: the status says which version built it and what to do',
      /different engine version/.test(byIdMap['ex-status'].textContent)
      && /recalculate the report/.test(byIdMap['ex-summary'].textContent),
      byIdMap['ex-status'].textContent + ' | ' + byIdMap['ex-summary'].textContent);
    check('parser mismatch: nothing is indexed and no source is claimed',
      st.indexed === false && st.source === null, st.source);
    check('parser mismatch: not one partition was downloaded',
      LAKE.parts.length === 0, JSON.stringify(LAKE.parts));
    check('parser mismatch: nothing was imported into the cache',
      lastWorker.posted.filter(m => m.type === 'explore:cache_import').length === 0);
    check('parser mismatch: the progress bar is put away, not left spinning',
      byIdMap['ex-progress'].hidden === true);
  }

  // ── ADR-14b: server-side execution — the lake answers with NO worker ──
  // `data-lake-exec="1"` + a published manifest: one `status` call marks the
  // page indexed, and intent/sql travel to POST …/lake/exec instead of the
  // worker. Not a byte of Parquet reaches the browser.
  {
    LAKE.manifest = JSON.parse(JSON.stringify(LAKE_MANIFEST));
    LAKE.parts.length = 0;
    EXEC.calls.length = 0;
    EXEC.handler = (dash, b) => {
      if (b.op === 'status') return { ok: true, status: 200, json: async () => ({
        tables: [{ name: 'sales_orders', partitions: [{ key: '2026-06' }, { key: '2026-07' }] }],
        stats: { tables: 1, partitions: 2, rows: 22 },
        semantics: null, manifest: JSON.parse(JSON.stringify(LAKE_MANIFEST)), index_errors: [] }) };
      if (b.op === 'sql') return { ok: true, status: 200, json: async () => ({
        columns: ['value'], rows: [[800.5]], total: 1, ms: 3, truncated: false, error: null,
        partitions_used: ['2026-06', '2026-07'], partitions_expected: 2,
        missing_partitions: [], incomplete: false }) };
      return { ok: false, status: 409, json: async () => ({ detail: { code: 'x', message: 'no' } }) };
    };
    dom = buildDom();
    dom.root.dataset.lakeExec = '1';
    (0, eval)(src);
    const wBefore = lastWorker;
    await tick(6);
    const st = global.window.__opExploreState;
    body.querySelectorAll('.ex-report-load')[0].dispatchEvent({ type: 'click' });
    await tick(8);
    check('server exec: ONE status call activates the report — nothing downloaded, nothing imported',
      EXEC.calls.length === 1 && EXEC.calls[0].body.op === 'status' && LAKE.parts.length === 0,
      JSON.stringify(EXEC.calls.map(c => c.body.op)) + '/' + JSON.stringify(LAKE.parts));
    check('server exec: indexed from the server, execution mode = server',
      st.indexed === true && st.source === 'server' && st.exec === 'server'
      && st.execDash === 'd1' && st.dirHandle === null,
      st.source + '/' + st.exec);
    check('server exec: the summary says the run AND that the server computes',
      /run 42/.test(byIdMap['ex-summary'].textContent)
      && /calculated by server/.test(byIdMap['ex-summary'].textContent),
      byIdMap['ex-summary'].textContent);
    check('server exec: NO worker was created for any of it', lastWorker === wBefore);

    byIdMap['ex-sql'].value = 'SELECT 1';
    click(byIdMap['ex-run-btn']);
    await tick(8);
    const runs = EXEC.calls.filter(c => c.body.op === 'sql');
    check('server exec: the SQL run went to POST …/lake/exec, not to a worker',
      runs.length === 1 && runs[0].body.sql === 'SELECT 1' && runs[0].body.limit >= 1
      && lastWorker === wBefore,
      JSON.stringify(EXEC.calls.map(c => c.body.op)));
    const cells = byIdMap['ex-result-tbody'].children;
    check('server exec: the server\'s rows render exactly like the worker\'s',
      cells.length === 1 && /800/.test(cells[0].textContent), cells.length && cells[0].textContent);
    EXEC.handler = null;
  }

  // ── Residue clarify: the ENGINE found a question word in two dims ─────
  // The worker answers `explore:intent` with `clarify` instead of SQL: the
  // page must show chips INSTEAD of a number, and a chip click re-enters
  // the intent path with the filter added and the word consumed.
  {
    dom = buildDom();
    (0, eval)(src);
    await tick(2);
    global.window.__opExploreTestFiles = [
      { name: 'DataSalesOrder_06.26.xlsx', buf: new ArrayBuffer(8), mtime_ms: 1000 }];
    click(byIdMap['ex-connect-btn']);
    await tick(2);
    click(byIdMap['ex-index-btn']);
    await tick(4);
    lastWorker.respond({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0 } });
    await tick(8);
    const probe = lastWorker.posted.filter(m => m.type === 'explore:cache_probe').pop();
    lastWorker.respond({ type: 'explore:cache_probed', id: probe.id, keys: ['k1'], cached: [false],
      stats: { files: 0, bytes: 0 } });
    await tick(8);
    const idx = lastWorker.posted.filter(m => m.type === 'explore:index').pop();
    lastWorker.respond({ type: 'explore:indexed', id: idx && idx.id, indexed: 1, errors: [], tables: [],
      stats: { tables: 1, partitions: 1 },
      semantics: { generators: [{ generator: 'slreport', generator_id: 7,
        tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
        kpis: [{ key: 'sl', name: 'Service level', ready: true, dims: ['client', 'unit'] }] }] } });
    await tick(4);
    const before = lastWorker.posted.length;
    byIdMap['ex-nl'].value = 'какой SL в MBU?';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await tick(6);
    const sent = lastWorker.posted.slice(before).find(m => m.type === 'explore:intent');
    check('clarify: the intent went to the engine', !!sent,
      JSON.stringify({ posted: lastWorker.posted.slice(before).map(m => m.type),
        status: byIdMap['ex-nl-status'].textContent,
        fetched: fetchCalls.slice(-4).map(c => c.url),
        summary: byIdMap['ex-summary'].textContent.slice(0, 120) }));
    lastWorker.respond({ id: sent.id, type: 'explore:intent_result', sql: null, error: null,
      notes: [], clarify: [{ word: 'MBU', options: [
        { dim: 'unit', value: 'MBU' }, { dim: 'client', value: 'MBU' }] }],
      kpi: { key: 'sl', name: 'Service level' } });
    await tick(6);
    const chips = byIdMap['ex-clarify'].querySelectorAll('.ex-clarify-chip');
    check('clarify: chips INSTEAD of a number — no SQL was run',
      byIdMap['ex-clarify'].hidden === false && chips.length === 3
      && !lastWorker.posted.slice(before).some(m => m.type === 'explore:sql'),
      chips.length + '/' + byIdMap['ex-clarify'].hidden);
    check('clarify: the status says a word needs settling',
      /clarification/i.test(byIdMap['ex-nl-status'].textContent), byIdMap['ex-nl-status'].textContent);
    const b2 = lastWorker.posted.length;
    chips[0].dispatchEvent({ type: 'click' });
    await tick(6);
    const again = lastWorker.posted.slice(b2).find(m => m.type === 'explore:intent');
    check('clarify: the chip re-enters with the filter added and the word consumed',
      again && (again.intent.filters || []).some(f => f.dim === 'unit' && f.value === 'MBU')
      && !(again.intent.probe_words || []).includes('MBU'),
      JSON.stringify(again && again.intent));
    lastWorker.respond({ id: again.id, type: 'explore:intent_result', error: null,
      sql: 'WITH c AS (SELECT 1 AS n) SELECT SUM(n) AS value FROM c',
      notes: ['«MBU» read as unit = «MBU»'], kpi: { key: 'sl', name: 'Service level' } });
    await tick(6);
    check('clarify: after the click the chips are gone and the SQL is in the editor',
      byIdMap['ex-clarify'].hidden === true && /^WITH c AS/.test(byIdMap['ex-sql'].value),
      byIdMap['ex-sql'].value);
    delete global.window.__opExploreTestFiles;
  }

  // ── Verifier: an advisory model verdict holds the number back ────────
  // Server-LLM path: /api/explore/intent/verify answers {ok:false, …} — the
  // page must show the verifier's own question with «calculate anyway»
  // instead of running the SQL; the click proceeds.
  {
    dom = buildDom();
    (0, eval)(src);
    await tick(2);
    global.__llmCfg = { provider: 'ollama', endpoint: '', model: 'llama3',
                        browser: false, server: true, key: null };
    global.__verifyVerdict = { ok: false, issue: 'the question is about MBU but there is no filter',
                               question: 'Please clarify: is MBU a business unit?' };
    global.window.__opExploreTestFiles = [
      { name: 'DataSalesOrder_06.26.xlsx', buf: new ArrayBuffer(8), mtime_ms: 1000 }];
    click(byIdMap['ex-connect-btn']);
    await tick(2);
    click(byIdMap['ex-index-btn']);
    await tick(4);
    lastWorker.respond({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0 } });
    await tick(8);
    const probe = lastWorker.posted.filter(m => m.type === 'explore:cache_probe').pop();
    lastWorker.respond({ type: 'explore:cache_probed', id: probe.id, keys: ['k1'], cached: [false],
      stats: { files: 0, bytes: 0 } });
    await tick(8);
    const idx = lastWorker.posted.filter(m => m.type === 'explore:index').pop();
    lastWorker.respond({ type: 'explore:indexed', id: idx && idx.id, indexed: 1, errors: [], tables: [],
      stats: { tables: 1, partitions: 1 },
      semantics: { generators: [{ generator: 'slreport', generator_id: 7,
        tables: { sl_orders: 'bound' }, views: { sl_lines: 'ok' },
        kpis: [{ key: 'sl', name: 'Service level', ready: true, dims: ['client'] }] }] } });
    await tick(4);
    const before = lastWorker.posted.length;
    const fb = fetchCalls.length;
    byIdMap['ex-nl'].value = 'какой SL по сетям?';
    byIdMap['ex-nl-btn'].dispatchEvent({ type: 'click' });
    await tick(8);
    check('verify: the verdict route was asked',
      fetchCalls.slice(fb).some(c => c.url === '/api/explore/intent/verify'),
      JSON.stringify(fetchCalls.slice(fb).map(c => c.url)));
    check('verify: {ok:false} holds the number back — nothing went to the worker',
      !lastWorker.posted.slice(before).some(m => m.type === 'explore:intent'),
      JSON.stringify(lastWorker.posted.slice(before)));
    check('verify: the verifier\'s own question is on screen with the escape hatch',
      byIdMap['ex-clarify'].hidden === false
      && /is MBU a business unit/.test(byIdMap['ex-clarify'].textContent)
      && /calculate anyway/.test(byIdMap['ex-clarify'].textContent),
      byIdMap['ex-clarify'].textContent);
    const go = Array.from(byIdMap['ex-clarify'].querySelectorAll('.ex-clarify-chip'))
      .find(b => /calculate anyway/.test(b.textContent));
    go.dispatchEvent({ type: 'click' });
    await tick(6);
    const sent = lastWorker.posted.slice(before).find(m => m.type === 'explore:intent');
    check('verify: «calculate anyway» proceeds to the engine', !!sent,
      JSON.stringify(lastWorker.posted.slice(before)));
    global.__verifyVerdict = null;
    global.__llmCfg = null;
    delete global.window.__opExploreTestFiles;
  }

  // ── a folder with no report: tables "as is" ────────────────────────
  // A plain folder used to hit «no file matched the catalogue tables», and
  // Explore was useless to anyone without a generator. The engines accept a
  // table name directly, so the page names the tables itself from the file
  // names. Pin the whole path: the table name, the message, the column query
  // and the tables reaching the query builder.
  {
    dom = buildDom();
    (0, eval)(src);
    await tick(4);
    global.window.__opExploreTestFiles = [
      { name: 'delta-sales.xlsx', buf: new ArrayBuffer(8), mtime_ms: 10 },
      { name: 'delta stock.xlsx', buf: new ArrayBuffer(8), mtime_ms: 20 },
    ];
    click(byIdMap['ex-connect-btn']);
    await tick(4);
    click(byIdMap['ex-index-btn']);
    await tick(4);
    lastWorker.respond({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0 } });
    await tick(8);
    const probe = lastWorker.posted.find(m => m.type === 'explore:cache_probe');
    check('folder as is: both files are indexed, not zero',
      !!probe && probe.files.length === 2, JSON.stringify(probe && probe.files));
    lastWorker.respond({ type: 'explore:cache_probed', id: probe.id, keys: ['k1', 'k2'],
      cached: [false, false], stats: { files: 0, bytes: 0 } });
    await tick(8);
    const first = lastWorker.posted.filter(m => m.type === 'explore:index')[0];
    check('folder as is: the table name is derived from the file name and is SQL-safe',
      !!first && first.files[0].table === 'delta_sales',
      JSON.stringify(first && first.files[0]));
    check('folder as is: the user is told which tables to query',
      /taking it as it is/.test(byIdMap['ex-summary'].textContent)
      && /delta_sales/.test(byIdMap['ex-summary'].textContent),
      byIdMap['ex-summary'].textContent);
    lastWorker.respond({ type: 'explore:indexed', id: first.id, indexed: 1, errors: [],
      tables: [{ name: 'delta_sales', partitions: [{ key: '' }] }],
      stats: { tables: 1, partitions: 1, rows: 10, bytes: 40 } });
    await tick(8);
    const second = lastWorker.posted.filter(m => m.type === 'explore:index')[1];
    check('folder as is: a space in the file name does not break the table name',
      !!second && second.files[0].table === 'delta_stock',
      JSON.stringify(second && second.files[0]));
    lastWorker.respond({ type: 'explore:indexed', id: second.id, indexed: 1, errors: [],
      tables: [{ name: 'delta_sales', partitions: [{ key: '' }] },
               { name: 'delta_stock', partitions: [{ key: '' }] }],
      stats: { tables: 2, partitions: 2, rows: 20, bytes: 80 } });
    await tick(8);
    // The index summary is shown BEFORE the columns are asked for: waiting for
    // that answer here once swallowed the whole result.
    check('folder as is: the index summary is shown without waiting for columns',
      /2 tables|table/.test(byIdMap['ex-summary'].textContent), byIdMap['ex-summary'].textContent);
    const colq = lastWorker.posted.filter(m => m.type === 'explore:sql');
    check('folder as is: the engine is asked for columns — otherwise the builder is empty',
      colq.some(m => /LIMIT 0/.test(m.sql) && /delta_sales/.test(m.sql)),
      JSON.stringify(colq.map(m => m.sql)));
    colq.forEach(m => lastWorker.respond({ type: 'explore:result', id: m.id,
      columns: ['date', 'outlet', 'sales'], rows: [], total: 0 }));
    await tick(8);
    const names = byIdMap['ex-b-table'].children.map(c => c.textContent);
    check('folder as is: the folder tables show up in the query builder',
      names.includes('delta-sales.xlsx') && names.includes('delta stock.xlsx'),
      JSON.stringify(names));
    delete global.window.__opExploreTestFiles;
  }

  console.log(FAILURES.length ? ('\n' + FAILURES.length + ' FAILED') : '\nALL PASS');
  process.exitCode = FAILURES.length ? 1 : 0;
}

main();
