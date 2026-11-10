/* Headless harness for static/js/explore-chat.js — the /explore Chat.
 *
 * Same idiom as tests/explore_dom.js: no jsdom, no new dependency. Two parts:
 *
 *   Part 1  the PURE half, required straight into node — the record shape,
 *           the ≤200-row trim, the 500-record cap, the key format and the
 *           follow-up matcher, driven with a fake backend.
 *   Part 2  the DOM half, eval'd over the smallest mock document it touches
 *           plus a stub `window.OP_EXPLORE`, so a bubble is really rendered
 *           and can be inspected — including the XSS pin: the chat is a
 *           CLIENT-ONLY surface (nothing it renders is ever stored on the
 *           server), so `tests/smoke_xss_escaping.py` has nothing to reach
 *           and the «user text is textContent, never innerHTML» guarantee is
 *           pinned HERE instead.
 *
 * Run: node tests/explore_chat_dom.js   (prints PASS/FAIL lines + ALL PASS)
 *
 * Deliberate Cyrillic: the chat's follow-up matcher, month parser, metric-word
 * stems and form scoring are written for RUSSIAN questions, so the questions
 * those paths read (and the KPI forms/synonyms they are scored against) stay
 * in Russian on purpose. Everything else here is English.
 */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const FAILURES = [];
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (!cond && extra ? '  <-- ' + extra : ''));
  if (!cond) FAILURES.push(label);
}

// ═══════════════════════════════════════════════════════════════════
// Part 1 — the pure half, no DOM at all
// ═══════════════════════════════════════════════════════════════════
const CHAT = require(path.join(ROOT, 'static', 'js', 'explore-chat.js'));

// A backend is three methods over a Map; the store's rules (key, next n, cap)
// are what is under test, not IndexedDB.
function fakeBackend() {
  const mem = new Map();
  return {
    mem: mem,
    async list(user, report) {
      return Array.from(mem.values()).filter(r => r.user === user
        && (report == null || String(r.report_id) === String(report)));
    },
    async put(key, rec) { mem.set(key, rec); },
    async del(key) { mem.delete(key); },
  };
}

// ── key format ────────────────────────────────────────────────────────
check('key: `${user}:${report}:${n}`', CHAT.chatKey(7, '9', 3) === '7:9:3', CHAT.chatKey(7, '9', 3));

// ── `#q=N` parse ──────────────────────────────────────────────────────
check('#q=N: a bare hash parses', CHAT.parseQHash('#q=7') === 7, String(CHAT.parseQHash('#q=7')));
check('#q=N: works without the leading #', CHAT.parseQHash('q=12') === 12, String(CHAT.parseQHash('q=12')));
check('#q=N: another key in the hash is not read as q', CHAT.parseQHash('#pres=3') === null,
  String(CHAT.parseQHash('#pres=3')));
check('#q=N: garbage -> null (screen A stays as it was)',
  CHAT.parseQHash('#q=abc') === null && CHAT.parseQHash('') === null && CHAT.parseQHash(null) === null);
check('#q=N: record 0 is not a record', CHAT.parseQHash('#q=0') === null);

// ── record shape ──────────────────────────────────────────────────────
(() => {
  const rec = CHAT.shapeRecord({
    user: 'u1', report_id: '7', n: 1, ts: 1750000000000,
    question: 'what was SL in July?', kind: 'kpi', source: 'ask',
    intent: { kpi: 'sl', generator_id: 7, dims: ['client'], filters: [{ dim: 'month', op: '=', value: 7 }] },
    kpi: { key: 'sl', name: 'Service level', short: 'SL', definition: 'Σmin/Σ', measure: 'ratio', dims: ['client'] },
    sql: 'SELECT 1',
    res: { columns: ['client', 'value'], rows: [['Client A', 0.9], ['Client B', 0.8]], total: 2 },
    provenance: { partitions_used: [{ table: 'orders', key: '2026-07' }], as_of: 1750000000000,
                  files: ['SL_07.xlsx'] },
    parent_n: null,
  });
  check('record: carries n, ts, question, report_id and kind',
    rec.n === 1 && rec.ts === 1750000000000 && rec.report_id === '7'
    && rec.question === 'what was SL in July?' && rec.kind === 'kpi', JSON.stringify(rec).slice(0, 200));
  check('record: cols and rows come from the result',
    JSON.stringify(rec.cols) === '["client","value"]' && rec.rows.length === 2, JSON.stringify(rec.cols));
  check('record: provenance is partitions_used + as_of + files',
    rec.provenance.partitions_used.length === 1 && rec.provenance.as_of === 1750000000000
    && rec.provenance.files[0] === 'SL_07.xlsx', JSON.stringify(rec.provenance));
  check('record: the intent is a plain-JSON copy, not the live object',
    rec.intent !== null && rec.intent.kpi === 'sl' && rec.intent.dims[0] === 'client',
    JSON.stringify(rec.intent));
  check('record: the KPI is trimmed to its labels — never the whole semantics node',
    rec.kpi.key === 'sl' && rec.kpi.name === 'Service level' && !('dims' in rec.kpi),
    JSON.stringify(rec.kpi));
  check('record: parent_n is null when nothing spawned it', rec.parent_n === null);
  check('record: an answer the server did not record has no answer_id',
    rec.answer_id === null, String(rec.answer_id));
})();

// ── P4: the server's id of the answer, the only handle a vote has ─────
(() => {
  const withId = CHAT.shapeRecord({ user: 'u1', report_id: '7', n: 1, ts: 1,
    question: 'q', res: { columns: [], rows: [] }, answer_id: '412' });
  check('record: answer_id is kept as a number', withId.answer_id === 412, String(withId.answer_id));
  const junk = CHAT.shapeRecord({ user: 'u1', report_id: '7', n: 1, ts: 1,
    question: 'q', res: { columns: [], rows: [] }, answer_id: 'DROP TABLE' });
  check('record: a non-numeric answer_id is dropped, never echoed',
    junk.answer_id === null, String(junk.answer_id));
})();

// ── ≤200-row trim ─────────────────────────────────────────────────────
(() => {
  const big = [];
  for (let i = 0; i < 5000; i++) big.push([i, i * 2]);
  const rec = CHAT.shapeRecord({ user: 'u', report_id: 'r', n: 1, question: 'q',
    res: { columns: ['a', 'b'], rows: big, total: 5000 } });
  check('trim: a record never holds more than 200 rows',
    rec.rows.length === CHAT.MAX_ROWS && CHAT.MAX_ROWS === 200, String(rec.rows.length));
  check('trim: it keeps the FIRST 200 (the order the engine returned)',
    rec.rows[0][0] === 0 && rec.rows[199][0] === 199, JSON.stringify(rec.rows[199]));
  check('trim: the record stays honest about the total it came from',
    rec.total === 5000 && rec.truncated === true, rec.total + '/' + rec.truncated);
  const small = CHAT.shapeRecord({ user: 'u', report_id: 'r', n: 1, question: 'q',
    res: { columns: ['a'], rows: [[1]], total: 1 } });
  check('trim: a small result is not marked truncated', small.truncated === false);
})();

// ── 500-cap eviction ──────────────────────────────────────────────────
(() => {
  const at = n => ({ n: n });
  check('evict: under the cap nothing goes',
    JSON.stringify(CHAT.evictions([at(1), at(2)], 500)) === '[]');
  check('evict: exactly at the cap, the OLDEST one makes room for the new one',
    JSON.stringify(CHAT.evictions([at(1), at(2), at(3)], 3)) === '[1]',
    JSON.stringify(CHAT.evictions([at(1), at(2), at(3)], 3)));
  check('evict: over the cap, every excess record goes, oldest first',
    JSON.stringify(CHAT.evictions([at(5), at(6), at(7), at(8)], 3)) === '[5,6]',
    JSON.stringify(CHAT.evictions([at(5), at(6), at(7), at(8)], 3)));
  check('evict: the default cap is 500', CHAT.MAX_RECORDS === 500, String(CHAT.MAX_RECORDS));
})();

// ── ChatStore over the fake backend ───────────────────────────────────
async function storeCases() {
  const backend = fakeBackend();
  const store = new CHAT.ChatStore({ backend: backend, user: 'u1', cap: 4 });
  for (let i = 1; i <= 3; i++) {
    await store.add('7', { question: 'q' + i, ts: 1000 + i,
      res: { columns: ['value'], rows: [[i]], total: 1 }, kind: 'kpi',
      intent: { kpi: 'sl' }, sql: 'S' + i });
  }
  check('store: n counts up from 1 inside a thread',
    (await store.list('7')).map(r => r.n).join(',') === '1,2,3',
    (await store.list('7')).map(r => r.n).join(','));
  check('store: the backend key is user:report:n',
    backend.mem.has('u1:7:1') && backend.mem.has('u1:7:3'), Array.from(backend.mem.keys()).join(' '));
  check('store: get(report, n) finds the record', (await store.get('7', 2)).question === 'q2');
  check('store: count is per thread', (await store.count('7')) === 3);

  // A second report is a second thread: its own n sequence, its own cap.
  await store.add('9', { question: 'another report', ts: 2000,
    res: { columns: ['value'], rows: [[1]] }, kind: 'kpi', intent: { kpi: 'rdc' } });
  check('store: a second report starts its own numbering at 1',
    (await store.list('9')).map(r => r.n).join(',') === '1',
    (await store.list('9')).map(r => r.n).join(','));
  check('store: threads() lists one row per report, newest first',
    (await store.threads()).map(t => t.report_id).join(',') === '9,7',
    (await store.threads()).map(t => t.report_id).join(','));

  // Cap 4: adding the 5th record to thread 7 evicts n=1.
  await store.add('7', { question: 'q4', ts: 1004, res: { columns: ['value'], rows: [[4]] }, kind: 'kpi' });
  await store.add('7', { question: 'q5', ts: 1005, res: { columns: ['value'], rows: [[5]] }, kind: 'kpi' });
  const left = await store.list('7');
  check('store: the cap evicts the OLDEST record of that thread only',
    left.map(r => r.n).join(',') === '2,3,4,5', left.map(r => r.n).join(','));
  check('store: the evicted key is gone from the backend', !backend.mem.has('u1:7:1'));
  check('store: the other thread is untouched by the eviction',
    (await store.list('9')).length === 1);
  check('store: n keeps counting up after an eviction (never reused)',
    left[left.length - 1].n === 5, String(left[left.length - 1].n));
}

// ── the follow-up matcher ─────────────────────────────────────────────
// prevIntent = the answer currently OPEN. The table below is the contract:
// a deterministic follow-up is answered client-side; anything naming a
// metric, or naming nothing the matcher understands, is not.
const PREV = { kpi: 'sl', generator_id: 7, dims: ['client'],
               filters: [{ dim: 'month', op: '=', value: 7 }, { dim: 'year', op: '=', value: 2026 }],
               order: { by: 'value', dir: 'desc' }, limit: 10 };
// Deliberate Cyrillic: the synonyms and forms the Russian follow-up matcher reads.
const CTX = {
  dims: ['client', 'sklad', 'year', 'month', 'week', 'day'],
  synonyms: { sklad: ['склад', 'складам'], client: ['сеть', 'сетям', 'клиент'] },
  forms: ['какой SL', 'уровень сервиса', 'доля реализованных заказов'],
};

function fu(q) { return CHAT.matchFollowUp(q, PREV, CTX); }

(() => {
  // Deliberate Cyrillic: Russian follow-ups, the language the matcher parses.
  const MATCHER = [
    // [question, expected dims (or null = no match), note]
    ['а по неделям?', ['week'], 'granularity: week'],
    ['по месяцам', ['month'], 'granularity: month'],
    ['а по дням?', ['day'], 'granularity: day'],
    ['по годам', ['year'], 'granularity: year'],
    ['а по складам?', ['sklad'], 'a dim by its own name'],
    ['а по сетям?', ['client'], 'a dim by a synonym from the semantics'],
    ['а в июне?', ['client'], 'period only — the cut is untouched'],
    ['за 2025', ['client'], 'a year only'],
    // ── negatives ──
    ['какой SL по неделям?', null, 'NAMES A METRIC -> must not match'],
    ['уровень сервиса по складам', null, 'a quoted question form is a new question'],
    ['доля реализованных заказов', null, 'ditto, with no cut at all'],
    ['а что вообще?', null, 'matches no pattern'],
    ['в 2026 году на какой неделе был самый низкий уровень сервиса у Василька?', null, 'a question of its own (interrogative) — prod 2026-08-28'],
    ['на какой неделе было хуже всего?', null, 'interrogative without a metric word is still a new question'],
    ['за весь 2026 год', ['client'], 'a year as a span — period only'],
    ['а по хабам?', null, 'a dim this KPI does not have'],
    ['', null, 'empty'],
  ];
  MATCHER.forEach(([q, dims, note]) => {
    const got = fu(q);
    const ok = dims === null ? got === null
      : (got !== null && JSON.stringify(got.dims) === JSON.stringify(dims));
    check('matcher: ' + JSON.stringify(q) + ' — ' + note, ok, JSON.stringify(got));
  });

  // What a match actually produces, beyond the dims.
  const week = fu('а по неделям?');
  check('matcher: a granularity follow-up keeps the previous period untouched',
    JSON.stringify(week.filters) === JSON.stringify(PREV.filters), JSON.stringify(week.filters));
  const june = fu('а в июне?');
  check('matcher: a period follow-up REPLACES the month, keeps the year',
    JSON.stringify(june.filters.filter(f => f.dim === 'month')) === '[{"dim":"month","op":"=","value":6}]'
    && june.filters.some(f => f.dim === 'year' && f.value === 2026), JSON.stringify(june.filters));
  const y25 = fu('за 2025');
  check('matcher: a year follow-up replaces the year and keeps the month',
    y25.filters.some(f => f.dim === 'year' && f.value === 2025)
    && y25.filters.some(f => f.dim === 'month' && f.value === 7), JSON.stringify(y25.filters));
  const span = fu('за весь 2026 год');
  check('matcher: "the whole of 2026" drops the month, keeps only the year (prod 2026-08-28: week 34 stayed)',
    JSON.stringify(span.filters) === '[{"dim":"year","op":"=","value":2026}]', JSON.stringify(span.filters));
  const wk = CHAT.matchFollowUp('за весь 2026 год', Object.assign({}, PREV, { filters: [{ dim: 'week', op: '=', value: 34 }, { dim: 'year', op: '=', value: 2026 }] }), CTX);
  check('matcher: … and the week too', JSON.stringify(wk.filters) === '[{"dim":"year","op":"=","value":2026}]', JSON.stringify(wk.filters));
  check('matcher: it never mutates the intent it was given',
    JSON.stringify(PREV.filters) === '[{"dim":"month","op":"=","value":7},{"dim":"year","op":"=","value":2026}]',
    JSON.stringify(PREV.filters));
  // Stand 2026-09-06: "and in 2025?" after this year's SL was taken as a
  // period cut, summed zero rows and rendered an empty card — while the form
  // matcher knows a 2025 KPI. A year a KPI form names is a metric, not a period.
  const CTX2025 = Object.assign({}, CTX, { forms: CTX.forms.concat(['SL за 2025 год', '2025 Total']) });
  check('matcher: a year that a KPI form names ("SL for 2025") is a NEW question, not a period cut',
    CHAT.matchFollowUp('а в 2025 году?', PREV, CTX2025) === null);
  check('matcher: … while a year no form names stays a period cut',
    JSON.stringify((CHAT.matchFollowUp('а в 2024 году?', PREV, CTX2025) || {}).filters || null).indexOf('"value":2024') >= 0);
  check('matcher: no open answer -> no follow-up, ever',
    CHAT.matchFollowUp('а по неделям?', null, CTX) === null);
  check('matcher: it reports WHICH pattern fired (the status line says so)',
    week.matched.indexOf('granularity') >= 0 && june.matched.indexOf('period') >= 0,
    JSON.stringify([week.matched, june.matched]));
})();

// hasMetricWords on its own — the guard the negatives above rest on.
check('metric words: a question quoting a form is flagged',
  CHAT.hasMetricWords('какой SL по неделям?', CTX.forms) === true);
check('metric words: a pure follow-up is not',
  CHAT.hasMetricWords('а по неделям?', CTX.forms) === false);
check('metric words: a period is never a metric word',
  CHAT.hasMetricWords('а в июле 2026?', CTX.forms) === false);
check('metric words: inflection is tolerated (genitive plural ~ nominative plural of "orders")',
  CHAT.hasMetricWords('покажи заказы', ['доля реализованных заказов']) === true);

// questionTimes — only what the question READS may become a filter.
// Deliberate Cyrillic: Russian month names are what questionTimes parses.
check('times: a month by Russian name', CHAT.questionTimes('а в июне?').month === 6);
check('times: a year as 20\\d\\d', CHAT.questionTimes('за 2025 год').year === 2025);
check('times: the locative of May is May, not March', CHAT.questionTimes('в мае').month === 5);
check('times: two months named -> ambiguous, so neither is taken',
  CHAT.questionTimes('июнь или июль').month === undefined,
  JSON.stringify(CHAT.questionTimes('июнь или июль')));
check('times: nothing named -> nothing', JSON.stringify(CHAT.questionTimes('а по неделям')) === '{}');

// ── P3a: what a card SAYS about itself (pure, same functions the card uses) ──
// The badge is the ONE thing a reader checks before believing a number, so
// it is pinned here rather than only through the rendered card.
(() => {
  const rec = (over) => Object.assign({ kind: 'kpi', source: 'form', cols: ['sklad', 'value'],
    rows: [['a', 1]], intent: { kpi: 'sl', dims: [], filters: [] } }, over);
  check('badge: a form match at or above the server\'s own threshold is "exact form"',
    CHAT.tierBadge(rec({ match: { score: 6, runner_up: 2 } })).text === 'exact form',
    CHAT.tierBadge(rec({ match: { score: 6 } })).text);
  // Not "=== 4": the number lives in app/services/nl2sql.py and the badge
  // means "the server would have answered from this form". Read it there, so
  // moving the threshold on one side fails HERE instead of in production.
  const NL2SQL = fs.readFileSync(path.join(ROOT, 'app', 'services', 'nl2sql.py'), 'utf8');
  const srvMin = Number((NL2SQL.match(/^FORM_MIN_SCORE\s*=\s*(\d+)/m) || [])[1]);
  check('badge: the threshold IS nl2sql.FORM_MIN_SCORE, read from nl2sql.py',
    srvMin > 0 && CHAT.FORM_SURE === srvMin, CHAT.FORM_SURE + ' vs ' + srvMin);
  check('badge: a form that barely matched says so instead of claiming certainty',
    CHAT.tierBadge(rec({ match: { score: 2, runner_up: 2 } })).text === 'the form matched loosely',
    CHAT.tierBadge(rec({ match: { score: 2 } })).text);
  check('badge: a model-chosen metric is "understood by meaning", and reads as one',
    CHAT.tierBadge(rec({ source: 'model', match: null })).text === 'understood by meaning'
    && CHAT.tierBadge(rec({ source: 'model' })).cls === 'is-model');
  check('badge: free SQL over the semantics says which tier it is',
    CHAT.tierBadge(rec({ kind: 'sql', source: 'semantic_sql' })).text === 'SQL from semantics');
  check('badge: a question that got no answer is "no answer", never a metric name',
    CHAT.tierBadge(rec({ kind: 'error', source: 'chat' })).text === 'no answer');
  check('confidence: the numbers are shown when there are numbers',
    CHAT.confidenceText(rec({ match: { score: 6, runner_up: 2 } })) === 'match 6, next 2',
    String(CHAT.confidenceText(rec({ match: { score: 6, runner_up: 2 } }))));
  check('confidence: nothing honest to say -> nothing said',
    CHAT.confidenceText(rec({ source: 'ask', match: null })) === null);
  // Adversarial P3b, major 2. `match` has ONE producer — nl2sql.match_forms —
  // and it returns a hit only above the threshold and with the lead: a form
  // record therefore always looks confident, and «score 2» is a record the
  // server cannot send. The reachable low-confidence case is a MODEL that
  // picked one metric out of the shortlist it was given.
  check('confidence: a form match the server would really send is NOT low confidence',
    CHAT.lowConfidence(rec({ source: 'form', match: { score: srvMin, runner_up: 1 } })) === false,
    String(CHAT.lowConfidence(rec({ source: 'form', match: { score: srvMin, runner_up: 1 } }))));
  check('confidence: a model choosing out of a shortlist of several IS low confidence',
    CHAT.lowConfidence(rec({ source: 'model', match: null, shortlist: ['sl', 'ship', 'rdc'] })) === 'model',
    String(CHAT.lowConfidence(rec({ source: 'model', match: null, shortlist: ['sl', 'ship'] }))));
  check('confidence: a model with no one else on the menu had no choice to warn about',
    CHAT.lowConfidence(rec({ source: 'model', match: null, shortlist: ['sl'] })) === false
    && CHAT.lowConfidence(rec({ source: 'model', match: null, shortlist: [] })) === false);
})();

// "Understood as" — names only. A card that printed a VALUE here would be
// showing the answer twice and the reading once.
(() => {
  const chips = CHAT.understood({
    kind: 'kpi', kpi: { short: 'SL' },
    intent: { kpi: 'sl', dims: ['sklad', 'week'],
              filters: [{ dim: 'month', op: '=', value: 7 }, { dim: 'client', op: '=', value: 'Client A' }] },
  });
  const by = {}; chips.forEach(([k, v]) => { by[k] = v; });
  check('understood: the metric is named by its short label', by['metric'] === 'SL', JSON.stringify(chips));
  check('understood: the cut is the non-time dims only', by['dimension'] === 'sklad', JSON.stringify(chips));
  check('understood: the period joins the time filter and the grain',
    by['period'] === 'month 7 · by week', String(by['period']));
  check('understood: a non-time filter is its own chip', by['client'] === 'Client A', JSON.stringify(chips));
  const plain = CHAT.understood({ kind: 'kpi', kpi: { short: 'SL' }, intent: { kpi: 'sl', dims: [], filters: [] } });
  check('understood: no cut at all is said out loud, not left blank',
    JSON.stringify(plain).indexOf("the metric's total") > 0, JSON.stringify(plain));
})();

// THE INVARIANT: no answer is ever a blank card.
(() => {
  const base = { kind: 'kpi', cols: ['sklad', 'value'], intent: { kpi: 'sl', dims: [],
    filters: [{ dim: 'month', op: '=', value: 7 }, { dim: 'client', op: '=', value: 'Client A' }] },
    shortlist: ['orders', 'sl'] };
  check('empty: zero rows is a question back, not an answer',
    CHAT.clarifyReason(Object.assign({}, base, { rows: [] })) === 'empty');
  check('empty: an aggregate over zero rows (NULL) is one too',
    CHAT.clarifyReason(Object.assign({}, base, { rows: [[null, null]] })) === 'null');
  check('empty: a real number is not a clarify',
    CHAT.clarifyReason(Object.assign({}, base, { rows: [['a', 0.9]] })) === null);
  check('empty: zero is a NUMBER — an answer of 0 must never become a clarify',
    CHAT.clarifyReason(Object.assign({}, base, { rows: [['a', 0]] })) === null);
  const opts = CHAT.clarifyOptions(Object.assign({}, base, { rows: [] }));
  const acts = opts.map(o => o.act + (o.arg ? ':' + o.arg : ''));
  check('empty: every option is something this browser can really run',
    acts.indexOf('allperiod') >= 0 && acts.indexOf('nofilter:client') >= 0
    && acts.indexOf('alt:orders') >= 0 && acts[acts.length - 1] === 'ask', acts.join(','));
  check('empty: the metric that WAS used is not offered as an alternative to itself',
    acts.indexOf('alt:sl') < 0, acts.join(','));
})();

// The coverage pill: computed off the index this browser already holds.
(() => {
  const txt = CHAT.coverageOf({ rows: 14294 }, [{ partitions: [{ key: 'week 23' }] }],
    (n) => String(n));
  check('coverage: the pill names the partition and the row count',
    txt === 'week 23 · 14294 rows', txt);
  check('coverage: nothing indexed -> no pill at all',
    CHAT.coverageOf({}, [], (n) => String(n)) === '', CHAT.coverageOf({}, [], String));
})();

// Follow-ups: the cuts this metric HAS and this answer did not use.
(() => {
  const fu = CHAT.followUps({ intent: { kpi: 'sl', dims: ['sklad'] } }, ['sklad', 'client', 'week', 'year']);
  check('follow-ups: the used cut is not offered again, and "by year" is not a follow-up',
    fu.join(',') === 'by client,by week', fu.join(','));
})();

// ═══════════════════════════════════════════════════════════════════
// P3b (pure) — the idempotency keys, the alternatives, the transcript
// ═══════════════════════════════════════════════════════════════════
(() => {
  // The two keys the whole of P3b's "one question, one record" rests on.
  // Deliberate Cyrillic: askKey normalises Russian questions (case, spacing).
  check('key: the same question with the same options is the same key',
    CHAT.askKey('what was SL in July?', { parentN: 2 })
    === CHAT.askKey('  What was SL in July?  ', { parentN: 2 }),
    CHAT.askKey('what was SL in July?', { parentN: 2 }));
  check('key: a clarification of ANOTHER card is another question',
    CHAT.askKey('by week?', { parentN: 1 }) !== CHAT.askKey('by week?', { parentN: 2 }));
  check('key: a pinned metric makes it another question too',
    CHAT.askKey('by week?', {}) !== CHAT.askKey('by week?', { fixedKpi: 'ship' }));
  check('key: "Regenerate" is never deduped against the first ask',
    CHAT.askKey('by week?', {}) !== CHAT.askKey('by week?', { regen: true }));
  check('key: the same words in ANOTHER conversation are another question',
    CHAT.askKey('what is SL?', { report: '7' }) !== CHAT.askKey('what is SL?', { report: '7#c2' })
    && CHAT.askKey('what is SL?', { report: '7' }) === CHAT.askKey('what is SL?', { report: '7' }));

  const ev = { question: 'what was SL in July?', parent_n: 2, regen: false };
  check('dedup key: question + parent + regen, and nothing else',
    CHAT.answerKey(ev) === CHAT.answerKey({ question: ev.question, parent_n: 2, kind: 'kpi' })
    && CHAT.answerKey(ev) !== CHAT.answerKey({ question: ev.question, parent_n: 3 })
    && CHAT.answerKey(ev) !== CHAT.answerKey({ question: ev.question, parent_n: 2, regen: true }),
    CHAT.answerKey(ev));

  // Conversations: a second thread of the same report, and every lookup that
  // needs the REPORT reads the base.
  check('conversations: the id carries the conversation, the base stays readable',
    CHAT.convKey('7', 'c17') === '7#c17' && CHAT.baseOf('7#c17') === '7'
    && CHAT.baseOf('7') === '7' && CHAT.convOf('7#c17') === 'c17' && CHAT.convOf('7') === null,
    CHAT.convKey('7', 'c17'));

  // The alternatives: the shortlist, scored against the question the way the
  // server's own `rank_kpis` scores it.
  // Deliberate Cyrillic: names/forms scored against Russian questions by fitScore.
  const KPIS = {
    sl: { key: 'sl', short: 'SL', name: 'Service level', question_forms: ['какой SL'] },
    ship: { key: 'ship', short: 'Shipments', name: 'Объём отгрузок', question_forms: ['объём отгрузок'] },
    rdc: { key: 'rdc', short: 'RDC', name: 'Ремонт', question_forms: ['сколько ремонтов'] },
  };
  const alts = CHAT.shortlistAlts({ question: 'какой объём отгрузок за июль?',
    intent: { kpi: 'sl' }, shortlist: ['sl', 'ship', 'rdc'] }, k => KPIS[k]);
  check('alternatives: the metric the answer used is never offered as an alternative to itself',
    alts.every(a => a.key !== 'sl'), JSON.stringify(alts));
  check('alternatives: a candidate the question shares no word with is not offered at all',
    alts.every(a => a.key !== 'rdc'), JSON.stringify(alts));
  check('alternatives: the one the question really names comes first, with a per cent',
    alts.length === 1 && alts[0].key === 'ship' && alts[0].pct === 100, JSON.stringify(alts));
  check('alternatives: no shortlist -> nothing offered',
    CHAT.shortlistAlts({ question: 'q', intent: { kpi: 'sl' } }, k => KPIS[k]).length === 0);
  // Adversarial P3b, minor 6: `rank_kpis` scores the KPI's DIMS too, so the
  // server can shortlist a candidate on a word this side then scored 0 —
  // and `shortlistAlts` dropped it as noise. Both halves score it now.
  check('alternatives: a candidate the server raised BY ITS DIMS is not scored to zero here',
    CHAT.fitScore('сколько по складам', { name: 'Ремонт', dims: ['склады'] }) >= 1,
    String(CHAT.fitScore('сколько по складам', { name: 'Ремонт', dims: ['склады'] })));
  check('alternatives: a long question word inside a question form is worth the server\'s +2',
    CHAT.fitScore('сколько ремонтов', { name: 'Ремонт', question_forms: ['сколько ремонтов'] })
    > CHAT.fitScore('сколько ремонтов', { name: 'Ремонт', question_forms: [] }),
    String(CHAT.fitScore('сколько ремонтов', { name: 'Ремонт', question_forms: ['сколько ремонтов'] })));

  // H10 — the period asked for is not in the index this browser holds.
  const empty = { intent: { kpi: 'sl', filters: [{ dim: 'month', op: '=', value: 7 },
                                                 { dim: 'year', op: '=', value: 2026 }] } };
  check('coverage: a month the loaded partitions do not mention is "outside coverage"',
    CHAT.periodOutsideCoverage(empty, ['2026-06']) === true);
  check('coverage: … and one they do mention is not',
    CHAT.periodOutsideCoverage(empty, ['2026-07', '2026-06']) === false);
  check('coverage: no period filter, or no index at all -> nothing is claimed',
    CHAT.periodOutsideCoverage({ intent: { filters: [] } }, ['2026-06']) === false
    && CHAT.periodOutsideCoverage(empty, []) === false);

  // Adversarial P3b, major 3: the three readings `key.indexOf(value)` got
  // wrong. A partition key is now parsed into the periods it stands for and
  // compared at the FILTERED granularity only.
  const wk = (dim, v) => ({ intent: { kpi: 'sl', filters: [{ dim: dim, op: '=', value: v }] } });
  check('coverage: a WEEK filter over MONTH partitions claims nothing ("27" is not July)',
    CHAT.periodOutsideCoverage(wk('week', 27), ['2026-06', '2026-07']) === false);
  check('coverage: a YEAR filter over WEEK partitions claims nothing ("2026" is not week 23)',
    CHAT.periodOutsideCoverage(wk('year', 2026), ['week 23']) === false);
  check('coverage: … and a week over months stays silent even when it is absent — '
    + 'monthly partitions cannot answer a question about weeks',
    CHAT.periodOutsideCoverage(wk('week', 5), ['2025-12']) === false);
  check('coverage: a week the WEEK partitions really lack is "outside coverage", one they hold is not',
    CHAT.periodOutsideCoverage(wk('week', 27), ['week 23', 'week 24']) === true
    && CHAT.periodOutsideCoverage(wk('week', 24), ['week 23', 'week 24']) === false);
  check('coverage: a key shaped like nothing we can read is never evidence',
    CHAT.periodOutsideCoverage(wk('month', 7), ['part-a', 'part-b']) === false);
  check('coverage: the key reader names the periods a key stands for',
    JSON.stringify(CHAT.partitionFacets('2026-06')) === '{"year":2026,"month":6}'
    && CHAT.partitionFacets('2026-06-03').day === 3
    && CHAT.partitionFacets('2026-W23').week === 23
    && CHAT.partitionFacets('week 23').week === 23
    // Deliberate Cyrillic: the key reader also understands Russian week labels.
    && CHAT.partitionFacets('неделя 23').week === 23
    && CHAT.partitionFacets('2026').year === 2026
    && CHAT.partitionFacets('warehouse-A') === null,
    JSON.stringify(CHAT.partitionFacets('2026-06-03')));

  // A cut the metric does not have.
  check('dims: a cut the metric does not have is named, the ones it has are not',
    CHAT.unavailableDims({ intent: { dims: ['hub', 'sklad'], filters: [{ dim: 'client', value: 'Client A' }] } },
      ['client', 'sklad', 'month']).join(',') === 'hub',
    CHAT.unavailableDims({ intent: { dims: ['hub', 'sklad'], filters: [] } }, ['client', 'sklad']).join(','));
  check('dims: a metric whose cuts are unknown accuses nothing',
    CHAT.unavailableDims({ intent: { dims: ['hub'] } }, []).length === 0);

  // The transcript: questions and numbers, never the SQL.
  const TR = CHAT.transcriptOf([
    { question: 'what was SL in July?', kind: 'kpi', cols: ['value'], rows: [[0.87]],
      sql: 'SELECT sl FROM t', kpi: { key: 'sl' } },
    { question: 'by warehouse?', kind: 'kpi', cols: ['sklad', 'value'],
      rows: [['sklad-1', 0.9], ['sklad-2', 0.8]], sql: 'SELECT sklad FROM t' },
    { question: 'regenerated one', kind: 'kpi', cols: ['value'], rows: [[1]], superseded: true },
  ], v => String(v));
  check('transcript: every question is "Q:" and every answer "A:"',
    /Q: what was SL in July\?/.test(TR) && /A: 0.87/.test(TR)
    && /Q: by warehouse\?/.test(TR), TR.slice(0, 120));
  check('transcript: NO SQL ever goes into it', TR.indexOf('SELECT') < 0, TR);
  // Adversarial P3b, major 4: "Copy conversation" used to paste up to
  // five LABELLED rows per answer — the values of the dim columns, i.e. the
  // data this pane promises never to hand anywhere. A table is named and
  // counted; the scalar answer above is still the number itself.
  check('transcript: a table answer is named and counted, never quoted',
    /A: table, 2 rows/.test(TR), TR);
  check('transcript: not ONE row label of the result is in it',
    TR.indexOf('sklad-1') < 0 && TR.indexOf('sklad-2') < 0 && TR.indexOf('0.9') < 0, TR);
  check('transcript: a superseded answer is not pasted twice',
    TR.indexOf('regenerated one') < 0, TR);

  // The low-confidence rule the card offers alternatives on — over a record
  // the ROUTER can really produce (adversarial P3b, major 2): a model that
  // chose "sl" out of a menu that also held "ship".
  const lowRec = { source: 'model', question: 'какой объём отгрузок за июль?',
    intent: { kpi: 'sl' }, shortlist: ['sl', 'ship'] };
  check('confidence: a model choosing out of a shortlist is low confidence, and names the rival',
    CHAT.lowConfidence(lowRec) === 'model'
    && CHAT.shortlistAlts(lowRec, k => KPIS[k]).map(a => a.key).join(',') === 'ship',
    String(CHAT.lowConfidence(lowRec)) + ' / '
      + JSON.stringify(CHAT.shortlistAlts(lowRec, k => KPIS[k])));
})();

// ═══════════════════════════════════════════════════════════════════
// Part 2 — the DOM half: a real bubble, rendered
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
    this.children = []; this.parent = null; this.attrs = {}; this.style = {};
    this.dataset = {}; this.listeners = {};
    this._class = new ClassList(); this._text = '';
    this.hidden = false; this.value = ''; this.disabled = false;
    // The pin this whole file exists for: assigning innerHTML anywhere must
    // be visible to the harness. Nothing in explore-chat.js may set it.
    Object.defineProperty(this, 'innerHTML', {
      get: () => this._html || '',
      set: (v) => { this._html = String(v); INNER_HTML_WRITES.push({ tag: this.tagName, v: String(v) }); },
      configurable: true,
    });
  }
  get classList() { return this._class; }
  set className(v) { this._class.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this._class.value; }
  set textContent(v) { this.children = []; this._text = v === '' ? '' : String(v); }
  get textContent() { return this._text || this.children.map(collectText).join(' '); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatchEvent(ev) { ev.target = ev.target || this; (this.listeners[ev.type] || []).forEach(f => f.call(this, ev)); return true; }
  querySelectorAll(sel) { const out = []; walk(this, n => { if (matches(n, sel)) out.push(n); }); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const INNER_HTML_WRITES = [];
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

const byIdMap = {};
const bodyEl = new El('div');
function mk(tag, id) {
  const e = new El(tag);
  if (id) { e.attrs.id = id; byIdMap[id] = e; }
  bodyEl.appendChild(e);
  return e;
}
const docListeners = {};
global.document = {
  createElement: t => new El(t),
  getElementById: id => byIdMap[id] || null,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  querySelectorAll: sel => bodyEl.querySelectorAll(sel),
  querySelector: sel => bodyEl.querySelector(sel),
  createTextNode: v => ({ nodeType: 3, text: String(v) }),
  body: bodyEl,
};

// The four screen-A sections the chat toggles against, plus the chat itself.
['ex-reports', 'ex-ask', 'ex-answer', 'ex-rows'].forEach(id => mk('section', id));
const chatSection = mk('section', 'ex-chat'); chatSection.hidden = true;
mk('button', 'ex-chat-btn');
mk('span', 'ex-chat-count');
mk('div', 'ex-chat-threads');
mk('div', 'ex-chat-thread');
mk('textarea', 'ex-chat-input');
mk('button', 'ex-chat-send');
mk('button', 'ex-chat-clear');
mk('div', 'ex-chat-chips');
mk('div', 'ex-chat-status');
// The folder pill in the chat head + the one button that does the next step
// (phase 9.4 gives it a third state: indexed from the server, nothing to do).
mk('span', 'ex-chat-folder');
mk('button', 'ex-chat-folder-btn');
// The P3a head: the coverage pill, the «⋯» menu, the two pane buttons.
mk('span', 'ex-chat-cov');
mk('span', 'ex-chat-cov-text');
mk('button', 'ex-chat-more');
const menuEl = mk('div', 'ex-chat-menu'); menuEl.hidden = true;
['new', 'hist', 'copy', 'clear'].forEach(m => {
  const b = new El('button'); b.setAttribute('data-m', m); menuEl.appendChild(b);
});
mk('button', 'ex-chat-wide');
mk('button', 'ex-chat-close');
const navEl = mk('div', 'ex-chat-nav'); navEl.hidden = true;
mk('button', 'ex-chat-back');
mk('button', 'ex-chat-prev');
mk('button', 'ex-chat-next');
const recalcEl = mk('button', 'ex-chat-recalc'); recalcEl.hidden = true;

// ── a stub OP_EXPLORE: the page API explore-chat.js is written against ──
const answerListeners = [];
const OP_LISTENERS = {};
const emitOP = (evt, payload) => (OP_LISTENERS[evt] || []).forEach(fn => fn(payload));
const CALLS = { finishIntent: [], ask: [], whyCard: [], renderResult: [] };
const SEMANTICS = [{
  generator: 'slreport', generator_id: 7,
  kpis: [{ key: 'sl', short: 'SL', name: 'Service level', ready: true,
           dims: ['client', 'sklad', 'month', 'year', 'week'],
           question_forms: ['какой SL', 'уровень сервиса'] },
         // P3b needs a SECOND ready metric: "Refine" offers the shortlist,
         // and a shortlist of one metric offers nothing. Its cuts are
         // deliberately narrower than SL's (no week) — that is what proves a
         // clarification ran against the card it was opened on.
         // Deliberate Cyrillic: name/forms scored against the Russian questions below.
         { key: 'ship', short: 'Shipments', name: 'Объём отгрузок', ready: true,
           dims: ['client', 'sklad', 'month'],
           question_forms: ['объём отгрузок'] }],
}];
const OP = {
  state: { reportGen: 7, indexed: true, openAnswer: null },
  on: (evt, fn) => {
    if (evt === 'answer') answerListeners.push(fn);
    (OP_LISTENERS[evt] = OP_LISTENERS[evt] || []).push(fn);
    return () => { const i = answerListeners.indexOf(fn); if (i >= 0) answerListeners.splice(i, 1); };
  },
  // The real ask() emits ONE 'answer' per answered question; a provider
  // error returns true too but emits nothing (`ASK_SILENT` plays that).
  ask: async (q, opts) => {
    CALLS.ask.push({ q, opts });
    if (!OP.ASK_SILENT) answerListeners.slice().forEach(fn => fn({ question: q, kind: 'kpi', res: { rows: [] } }));
    return true;
  },
  ASK_SILENT: false,
  runQuery: async () => {},
  renderResult: (res) => { CALLS.renderResult.push(res); },
  // The real finishIntent emits ONE 'answer' for the intent it ran (the
  // chat now requires that — a follow-up that lands nothing is a failure record).
  finishIntent: async (intent, gen, expl) => {
    CALLS.finishIntent.push({ intent, gen, expl });
    if (!OP.ASK_SILENT) answerListeners.slice().forEach(fn => fn({ question: OP.state.askQuestion || '', kind: 'kpi', intent: intent, res: { rows: [] } }));
    return true;
  },
  whyCard: async (intent, gen, why) => { CALLS.whyCard.push({ intent, gen, why }); return true; },
  shiftPeriod: (f) => [{ dim: 'month', op: '=', value: 6 }],
  readySemantics: () => SEMANTICS,
  withGen: (i, g) => Object.assign({}, i, { generator_id: g.generator_id }),
  kpiOf: (g, key) => (g.kpis || []).find(k => k.key === key) || {},
  metricChips: (container, onPick) => { container.textContent = ''; return []; },
  setAnswerTitle: (t) => { CALLS.title = t; },
  setSqlBox: (v) => { CALLS.sql = v; },
  fmtNum: (v) => String(v),
  answerProvenance: () => ({ partitions_used: [{ table: 'orders', key: '2026-07' }],
                             as_of: 1750000000000, files: ['SL_07.xlsx'] }),
  userId: 'u1',
  reportClientError: (stage, reason, extra) => { CALLS.clientErrors.push({ stage, reason, extra }); },
};
CALLS.clientErrors = [];

const chatBackend = fakeBackend();
const winListeners = {};
global.window = { OP_EXPLORE: OP, __opChatBackend: chatBackend,
  addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); } };
// Setting location.hash fires `hashchange` in a browser; the mock needs it
// driven by hand, which is also what makes the assertion honest — the page
// must react to the EVENT, not to the click that set the hash.
function fireHash() { (winListeners.hashchange || []).forEach(f => f()); }
global.location = { hostname: 'localhost', hash: '', pathname: '/explore', search: '' };
global.history = { replaceState: () => {} };
global.fetch = async (url, opts) => {
  CALLS.fetch = { url, opts };
  return { ok: true, status: 200, json: async () => ({ id: 1 }) };
};
global.IDBKeyRange = { bound: () => ({}), only: () => ({}) };

(0, eval)(fs.readFileSync(path.join(ROOT, 'static', 'js', 'explore-chat.js'), 'utf8'));

function click(e) { e.dispatchEvent({ type: 'click', target: e }); }
// The document-level listeners (the "⋯" menu closes on an outside click and
// on Escape) have no element to be dispatched on — the mock drives them here.
function fireDoc(type, ev) { (docListeners[type] || []).forEach(f => f(Object.assign({ type: type }, ev))); }
async function tick(n) { for (let i = 0; i < (n || 6); i++) await new Promise(r => setTimeout(r, 0)); }

// The payload the page emits — with a question carrying markup, which is the
// whole XSS pin: a `<script>` a user typed must survive as TEXT.
const XSS_Q = '<script>alert(1)</script> what was SL in July?';

async function domCases() {
  // ── the toggle ─────────────────────────────────────────────────────
  check('chat: #ex-chat is hidden until the button is pressed', chatSection.hidden === true);
  click(byIdMap['ex-chat-btn']);
  await tick();
  check('chat: the button shows the chat', chatSection.hidden === false);
  check('chat: and hides every screen-A section',
    ['ex-reports', 'ex-ask', 'ex-answer', 'ex-rows'].every(id => byIdMap[id].hidden === true),
    ['ex-reports', 'ex-ask', 'ex-answer', 'ex-rows'].map(id => id + '=' + byIdMap[id].hidden).join(' '));
  click(byIdMap['ex-chat-btn']);
  await tick();
  check('chat: pressing it again goes back to screen A',
    chatSection.hidden === true && byIdMap['ex-ask'].hidden === false);

  // ── an answer becomes a bubble ──────────────────────────────────────
  answerListeners.forEach(fn => fn({
    question: XSS_Q, kind: 'kpi', source: 'ask',
    intent: { kpi: 'sl', generator_id: 7, dims: ['sklad'],
              filters: [{ dim: 'month', op: '=', value: 7 }] },
    kpi: { key: 'sl', name: 'Service level', short: 'SL' },
    sql: 'SELECT 1',
    res: { columns: ['sklad', 'value'], rows: [['Northport', 0.91], ['Eastfield', 0.72], ['Southgate', 0.5], ['Westbay', 0.4]], total: 9 },
  }));
  await tick(8);
  check('answer event: one record landed in the thread', chatBackend.mem.size === 1,
    String(chatBackend.mem.size));
  const stored = Array.from(chatBackend.mem.values())[0];
  check('answer event: the record is keyed by the pinned report',
    Array.from(chatBackend.mem.keys())[0] === 'u1:7:1', Array.from(chatBackend.mem.keys())[0]);
  check('answer event: the provenance came from the page, not from the record itself',
    stored.provenance.files[0] === 'SL_07.xlsx', JSON.stringify(stored.provenance));
  check('answer event: #ex-chat-count tracks the thread length',
    byIdMap['ex-chat-count'].textContent === '1', byIdMap['ex-chat-count'].textContent);

  click(byIdMap['ex-chat-btn']);
  await tick(8);
  const thread = byIdMap['ex-chat-thread'];
  check('bubbles: the thread rendered one bubble', thread.children.length === 1,
    String(thread.children.length));
  const bub = thread.children[0];

  // ── THE XSS PIN (client-side, so it lives here and not in
  //    tests/smoke_xss_escaping.py — the chat never stores anything server
  //    side, so that file has no surface to reach) ──────────────────────
  const q = bub.querySelector('.ex-bub-q');
  check('xss: the question is rendered via textContent, verbatim',
    q && q.textContent === XSS_Q, q && q.textContent);
  check('xss: NOTHING in explore-chat.js ever writes innerHTML',
    INNER_HTML_WRITES.length === 0, JSON.stringify(INNER_HTML_WRITES.slice(0, 2)));
  check('xss: the markup never became a child element',
    bub.querySelectorAll('script').length === 0);
  check('xss: and the source file never ASSIGNS innerHTML/outerHTML/insertAdjacentHTML',
    !/(inner|outer)HTML\s*=|insertAdjacentHTML/.test(
      fs.readFileSync(path.join(ROOT, 'static', 'js', 'explore-chat.js'), 'utf8')));

  // ── bubble shapes ───────────────────────────────────────────────────
  const bars = bub.querySelectorAll('.ex-bub-bar-fill');
  check('bubble: a multi-row answer renders one mini-bar per row shown (at most 8)',
    bars.length === 4, String(bars.length));
  check('bubble: the widest bar is the biggest value, as a % width',
    bars[0].style.width === '100.0%' && bars[1].style.width !== '100.0%',
    bars.map(b => b.style.width).join(','));
  check('bubble: the rest is "… N more" against the real total',
    bub.textContent.indexOf('… 5 more') >= 0, bub.textContent.slice(0, 300));
  check('bubble: the meta line credits the partitions and the data date',
    /partitions/.test(bub.textContent) && /data as of/.test(bub.textContent),
    bub.textContent.slice(-200));
  const acts = bub.querySelectorAll('[data-act]').map(b => b.getAttribute('data-act'));
  check('bubble: all four actions are offered on a KPI answer',
    acts.join(',') === 'expand,why,compare,pin', acts.join(','));

  // ── a one-number answer ─────────────────────────────────────────────
  answerListeners.forEach(fn => fn({
    question: 'July total', kind: 'kpi', source: 'ask',
    intent: { kpi: 'sl', generator_id: 7, dims: [], filters: [] },
    kpi: { key: 'sl' }, sql: 'SELECT 2',
    res: { columns: ['value'], rows: [[0.87]], total: 1 },
  }));
  await tick(8);
  check('bubble: a single-row answer is ONE number, no bars',
    byIdMap['ex-chat-thread'].children.length === 2
    && byIdMap['ex-chat-thread'].children[1].querySelectorAll('.ex-bub-num').length === 1
    && byIdMap['ex-chat-thread'].children[1].querySelectorAll('.ex-bub-bar-fill').length === 0,
    String(byIdMap['ex-chat-thread'].children.length));

  // ── a why-answer is three lines ─────────────────────────────────────
  answerListeners.forEach(fn => fn({
    question: 'why did SL drop?', kind: 'why', source: 'why', parent_n: 1,
    intent: { kpi: 'sl', generator_id: 7, dims: [], filters: [{ dim: 'month', op: '=', value: 7 }] },
    kpi: { key: 'sl' }, sql: 'SELECT 3',
    res: { columns: ['cut', 'member', 'value'], rows: [['sklad', 'Northport', 0.9]], total: 1 },
    why: { label: 'SL', period: 'July', current: 0.87, previous: 0.91, delta: '-0.04', shifted: true },
  }));
  await tick(8);
  const whyBub = byIdMap['ex-chat-thread'].children[2];
  check('bubble: a why-answer renders the card\'s own three lines',
    whyBub.querySelectorAll('.ex-bub-line').length === 3,
    String(whyBub.querySelectorAll('.ex-bub-line').length));
  check('bubble: the why lines carry the metric, the delta and the concentration',
    /SL for July = 0.87/.test(whyBub.textContent) && /previous period: 0.91/.test(whyBub.textContent)
    && /-0.04/.test(whyBub.textContent), whyBub.textContent.slice(0, 250));

  // ── the deterministic follow-up: no server, no model ────────────────
  OP.state.openAnswer = { question: 'what was SL in July?',
    intent: { kpi: 'sl', generator_id: 7, dims: [], filters: [{ dim: 'month', op: '=', value: 7 }] } };
  // Deliberate Cyrillic: a Russian follow-up, the language matchFollowUp parses.
  const askBefore = CALLS.ask.length, finishBefore = CALLS.finishIntent.length;
  byIdMap['ex-chat-input'].value = 'а по неделям?';
  click(byIdMap['ex-chat-send']);
  await tick(8);
  check('follow-up: "by week?" never reaches OP_EXPLORE.ask (no round trip)',
    CALLS.ask.length === askBefore, String(CALLS.ask.length - askBefore));
  check('follow-up: it goes straight through finishIntent',
    CALLS.finishIntent.length === finishBefore + 1, String(CALLS.finishIntent.length));
  const fi = CALLS.finishIntent[CALLS.finishIntent.length - 1];
  check('follow-up: the metric is inherited and the cut becomes the week',
    fi.intent.kpi === 'sl' && JSON.stringify(fi.intent.dims) === '["week"]',
    JSON.stringify(fi.intent));
  check('follow-up: the previous period rides along untouched',
    JSON.stringify(fi.intent.filters) === '[{"dim":"month","op":"=","value":7}]',
    JSON.stringify(fi.intent.filters));
  check('follow-up: the thread record will say it was a follow-up',
    OP.state.askSource === 'followup', String(OP.state.askSource));
  check('follow-up: the input is cleared for the next one',
    byIdMap['ex-chat-input'].value === '', byIdMap['ex-chat-input'].value);

  // A question that NAMES a metric must take the normal route, with the open
  // answer's intent as context — never the client shortcut. (Russian on purpose:
  // hasMetricWords matches it against the Russian question forms.)
  byIdMap['ex-chat-input'].value = 'какой уровень сервиса по складам?';
  click(byIdMap['ex-chat-send']);
  await tick(8);
  check('follow-up: a question naming a metric goes through ask() instead',
    CALLS.ask.length === askBefore + 1
    && CALLS.finishIntent.length === finishBefore + 1,
    CALLS.ask.length + '/' + CALLS.finishIntent.length);
  check('follow-up: and carries the OPEN answer as prev_intent',
    (CALLS.ask[CALLS.ask.length - 1].opts || {}).prevIntent
    && CALLS.ask[CALLS.ask.length - 1].opts.prevIntent.kpi === 'sl',
    JSON.stringify(CALLS.ask[CALLS.ask.length - 1].opts));

  // ── a question the provider rejected: true from ask(), no 'answer' ──
  // Prod, 2026-08-27: "I type, press send and the message disappears" — the
  // key was rejected, explore's own status line said so, the chat cleared
  // the box and showed nothing.
  {
    const nl = mk('div', 'ex-nl-status');
    nl.textContent = 'The api.deepseek.com key was rejected (401). Check the key.';
    OP.ASK_SILENT = true;
    const askN = CALLS.ask.length;
    byIdMap['ex-chat-input'].value = 'what is the service level by client?';
    click(byIdMap['ex-chat-send']);
    await tick(8);
    OP.ASK_SILENT = false;
    check('silent ask: the question did go to ask()', CALLS.ask.length === askN + 1);
    check('silent ask: the text STAYS in the box',
      byIdMap['ex-chat-input'].value === 'what is the service level by client?', byIdMap['ex-chat-input'].value);
    check('silent ask: the chat shows the provider error instead of nothing',
      /401/.test(byIdMap['ex-chat-status'].textContent), byIdMap['ex-chat-status'].textContent);
    check('silent ask: the send button is live again', byIdMap['ex-chat-send'].disabled === false);
    check('silent ask: the umbrella row reaches the server log (chat_no_answer + reason)',
      CALLS.clientErrors.length === 1 && CALLS.clientErrors[0].stage === 'chat_no_answer'
      && /401/.test(CALLS.clientErrors[0].reason), JSON.stringify(CALLS.clientErrors));
    // Tier 2: the model wrote SQL, DuckDB refused it — explore.js says so on
    // the SQL row, not the question line.
    nl.textContent = '';
    const sq = mk('div', 'ex-sql-status');
    sq.textContent = 'Could not run the query.';
    OP.ASK_SILENT = true;
    byIdMap['ex-chat-input'].value = 'how many orders does Aromatnyy mir have?';
    click(byIdMap['ex-chat-send']);
    await tick(8);
    OP.ASK_SILENT = false;
    check('silent ask: a refused model SQL keeps the text too',
      byIdMap['ex-chat-input'].value === 'how many orders does Aromatnyy mir have?', byIdMap['ex-chat-input'].value);
    check('silent ask: … and the SQL row\'s reason reaches the chat',
      /run the query/.test(byIdMap['ex-chat-status'].textContent), byIdMap['ex-chat-status'].textContent);
    sq.textContent = '';
    byIdMap['ex-chat-input'].value = '';
  }

  // ── the controller never holds a question (stand 2026-09-06) ──────────
  // The verifier's doubt used to become a card that swallowed the NEXT typed
  // line as its reply; three real questions vanished. Now: proceed at once,
  // the doubt rides on the answer as a note, and every typed line is a bubble.
  const T = () => byIdMap['ex-chat-thread'];
  const bubbles = () => Array.from(T().querySelectorAll('.ex-bub[data-n]'));
  const lastBub = () => bubbles()[bubbles().length - 1];
  const txt = (e, sel) => Array.from((e || T()).querySelectorAll(sel)).map(x => x.textContent).join(' | ');
  {
    let proceeded = 0;
    emitOP('verify', { issue: 'no period named', question: 'For which period?', proceed: () => { proceeded++; },
      resume: () => {}, intent: { kpi: 'sl', generator_id: 7, dims: ['week'], filters: [] }, gen: SEMANTICS[0], asked: 'q' });
    await tick(4);
    check('verify: the controller proceeds by itself, no held card in the thread',
      proceeded === 1 && !T().querySelector('.ex-bub-verify'), String(proceeded));
    const before = bubbles().length;
    answerListeners.slice().forEach(fn => fn({ question: 'which week had the lowest SL', kind: 'kpi', res: { rows: [] } }));
    await tick(10);
    check('verify: the doubt travels with the answer as a note',
      bubbles().length === before + 1 && /Checker:.*no period named/.test(txt(lastBub(), '.ex-bub-note')), txt(lastBub(), '.ex-bub-note'));
    // The next typed line is handled on its own merits: a bare "2026" over an
    // open answer is a period follow-up (finishIntent), anything else is
    // ask() — either way it is NOT consumed by the controller.
    const handledN = CALLS.ask.length + CALLS.finishIntent.length;
    byIdMap['ex-chat-input'].value = '2026';
    click(byIdMap['ex-chat-send']);
    await tick(10);
    check('verify: a typed "2026" after the controller is handled as its own line, not swallowed',
      CALLS.ask.length + CALLS.finishIntent.length === handledN + 1, String(CALLS.ask.length + CALLS.finishIntent.length - handledN));
  }

  // ── no silent drops: a question is on screen at once and ends as a record ──
  {
    const n0 = bubbles().length;
    // A slow provider that then fails silently: while it is thinking, the
    // typed line must already be on screen; when it gives up, a record.
    const realAsk = OP.ask;
    let release = null;
    OP.ask = (q, opts) => { CALLS.ask.push({ q, opts }); return new Promise(r => { release = () => r(true); }); };
    byIdMap['ex-chat-input'].value = 'a question with no answer';
    click(byIdMap['ex-chat-send']);
    await tick(4);
    const pend = Array.from(T().children).filter(c => /ex-bub-pending/.test(c.className));
    check('no-drop: the typed line is a bubble while the provider is still thinking',
      pend.length >= 1 && /a question with no answer/.test(pend[pend.length - 1].textContent), String(pend.length));
    release(); await tick(12);
    OP.ask = realAsk;
    check('no-drop: a question that got no answer is a persistent "no answer" bubble',
      bubbles().length === n0 + 1 && /a question with no answer/.test(txt(lastBub(), '.ex-bub-q')) && /no answer/.test(txt(lastBub(), '.ex-bub-tag')),
      JSON.stringify({ n: bubbles().length - n0, tag: txt(lastBub(), '.ex-bub-tag') }));
    check('no-drop: … and the pending card is gone', !T().querySelector('.ex-bub-pending'));
    const n1 = bubbles().length;
    byIdMap['ex-chat-input'].value = 'first'; click(byIdMap['ex-chat-send']); await tick(10);
    byIdMap['ex-chat-input'].value = 'second'; click(byIdMap['ex-chat-send']); await tick(10);
    const qs = bubbles().slice(-2).map(b => txt(b, '.ex-bub-q'));
    check('no-drop: two questions in a row are two bubbles, in order',
      bubbles().length === n1 + 2 && qs[0] === 'first' && qs[1] === 'second', qs.join('|'));
    OP.state.indexed = false; OP.state.indexing = false; OP.state.lastKill = null; OP.state.source = null;
    const n2 = bubbles().length;
    byIdMap['ex-chat-input'].value = 'how many were ordered'; click(byIdMap['ex-chat-send']); await tick(10);
    check('no-drop: a refusal (no index) is a bubble with the reason',
      bubbles().length === n2 + 1 && /no answer/.test(txt(lastBub(), '.ex-bub-tag')) && txt(lastBub(), '.ex-bub-note').length > 5,
      txt(lastBub(), '.ex-bub-note'));
    OP.state.indexed = true;
  }

  // ── "Expand →" / #q=N ───────────────────────────────────────────
  click(byIdMap['ex-chat-thread'].children[0].querySelector('[data-act="expand"]'));
  check('expand: the action navigates to #q=1', location.hash === '#q=1', location.hash);
  fireHash();
  await tick(8);
  check('expand: screen A came back and the chat is hidden',
    chatSection.hidden === true && byIdMap['ex-answer'].hidden === false);
  check('expand: the answer was rehydrated from the record, not re-run',
    CALLS.title === XSS_Q && CALLS.sql === 'SELECT 1'
    && CALLS.renderResult[CALLS.renderResult.length - 1].rows.length === 4,
    JSON.stringify(CALLS.renderResult[CALLS.renderResult.length - 1] || {}).slice(0, 200));
  check('expand: the "← Back to conversation" bar is on screen', navEl.hidden === false);
  check('expand: "Recalculate" is offered while the folder is indexed',
    recalcEl.hidden === false, String(recalcEl.hidden));
  check('expand: prev is disabled on the first record, next is not',
    byIdMap['ex-chat-prev'].disabled === true && byIdMap['ex-chat-next'].disabled === false,
    byIdMap['ex-chat-prev'].disabled + '/' + byIdMap['ex-chat-next'].disabled);
  click(byIdMap['ex-chat-next']);
  check('expand: → moves to n+1', location.hash === '#q=2', location.hash);
  fireHash();
  await tick(8);
  check('expand: and n+1 really is the next record',
    CALLS.title === 'July total', String(CALLS.title));
  click(byIdMap['ex-chat-back']);
  await tick(4);
  check('expand: "← Back to conversation" returns to the thread', chatSection.hidden === false);

  // ── "Pin" uses the endpoint that already exists ───────────────
  click(byIdMap['ex-chat-thread'].children[0].querySelector('[data-act="pin"]'));
  await tick(4);
  check('pin: POSTs to the existing /api/explore/questions',
    CALLS.fetch && CALLS.fetch.url === '/api/explore/questions'
    && CALLS.fetch.opts.method === 'POST', JSON.stringify(CALLS.fetch || {}).slice(0, 200));
  check('pin: sends Content-Type (OriginGuard refuses a body without one)',
    CALLS.fetch.opts.headers['Content-Type'] === 'application/json',
    JSON.stringify(CALLS.fetch.opts.headers));
  check('pin: the SQL it pins is the record\'s own',
    JSON.parse(CALLS.fetch.opts.body).sql === 'SELECT 1', CALLS.fetch.opts.body);

  // ── "Why?" / "Compare" ──────────────────────────────────────────
  click(byIdMap['ex-chat-thread'].children[0].querySelector('[data-act="why"]'));
  await tick(4);
  check('why: the bubble action runs the deterministic why-card over the same metric',
    CALLS.whyCard.length === 1 && CALLS.whyCard[0].intent.kpi === 'sl'
    && JSON.stringify(CALLS.whyCard[0].intent.dims) === '[]', JSON.stringify(CALLS.whyCard[0] || {}));
  check('why: the new record hangs off the bubble it was asked from',
    OP.state.askParentN === 1, String(OP.state.askParentN));

  const finishBefore2 = CALLS.finishIntent.length;
  click(byIdMap['ex-chat-thread'].children[0].querySelector('[data-act="compare"]'));
  await tick(4);
  check('compare: runs the SAME intent over the shifted period',
    CALLS.finishIntent.length === finishBefore2 + 1
    && JSON.stringify(CALLS.finishIntent[finishBefore2].intent.filters)
       === '[{"dim":"month","op":"=","value":6}]',
    JSON.stringify(CALLS.finishIntent[finishBefore2] || {}));
  check('compare: the record is marked as one, so the bubble can print both numbers',
    OP.state.askSource === 'compare', String(OP.state.askSource));

  // Two records that are parent/child by "Compare" print "was → now".
  answerListeners.forEach(fn => fn({
    question: 'July total — previous period', kind: 'kpi', source: 'compare', parent_n: 2,
    intent: { kpi: 'sl', generator_id: 7, dims: [], filters: [{ dim: 'month', op: '=', value: 6 }] },
    kpi: { key: 'sl' }, sql: 'SELECT 4',
    res: { columns: ['value'], rows: [[0.91]], total: 1 },
  }));
  await tick(8);
  const cmpBub = byIdMap['ex-chat-thread'].children[byIdMap['ex-chat-thread'].children.length - 1];
  check('compare: the bubble puts the two numbers side by side',
    /was 0.87 → now 0.91/.test(cmpBub.textContent), cmpBub.textContent.slice(0, 200));

  // ── "Clear chat": the thread is gone from IndexedDB, the count is 0 ──
  {
    const before = byIdMap['ex-chat-thread'].children.length;
    global.confirm = () => true;
    click(byIdMap['ex-chat-clear']);
    await tick(8);
    check('clear chat: the thread had bubbles before', before > 0, String(before));
    check('clear chat: not one bubble is left afterwards',
      byIdMap['ex-chat-thread'].querySelectorAll('.ex-bub').length === 0,
      String(byIdMap['ex-chat-thread'].querySelectorAll('.ex-bub').length));
    check('clear chat: … and the empty thread teaches instead of showing nothing',
      byIdMap['ex-chat-thread'].querySelectorAll('.ex-chat-sugg').length === 1
      && byIdMap['ex-chat-thread'].querySelectorAll('[data-chat-act="suggest"]').length === 3,
      String(byIdMap['ex-chat-thread'].querySelectorAll('[data-chat-act="suggest"]').length));
    // Deliberate Cyrillic: the fixture's own (Russian) question form.
    check('clear chat: the suggestions are the report\'s OWN question forms',
      /какой SL/.test(byIdMap['ex-chat-thread'].querySelector('.ex-chat-sugg').textContent),
      byIdMap['ex-chat-thread'].querySelector('.ex-chat-sugg').textContent);
    check('clear chat: the header count follows', byIdMap['ex-chat-count'].textContent === '0',
      byIdMap['ex-chat-count'].textContent);
    check('clear chat: the status says so', /cleared/.test(byIdMap['ex-chat-status'].textContent),
      byIdMap['ex-chat-status'].textContent);
  }

  // ── P3a: the answer card ────────────────────────────────────────────
  // The thread is empty here (the "Clear chat" block above), so every
  // bubble below is one this section pushed.
  {
    const push = (ev) => answerListeners.slice().forEach(fn => fn(ev));
    const ANS = { question: 'what was SL in July?', kind: 'kpi', source: 'form',
      intent: { kpi: 'sl', generator_id: 7, dims: ['sklad'],
                filters: [{ dim: 'month', op: '=', value: 7 }] },
      kpi: { key: 'sl', short: 'SL', name: 'Service level' }, sql: 'SELECT sl FROM t',
      res: { columns: ['sklad', 'value'], rows: [['Northport', 0.91], ['Eastfield', 0.72]], total: 2 } };

    push(Object.assign({}, ANS, { match: { score: 6, runner_up: 2, form: 'what is SL' } }));
    await tick(10);
    const card = lastBub();
    check('card: a confident form match is badged "exact form"',
      txt(card, '.ex-bub-tag') === 'exact form', txt(card, '.ex-bub-tag'));
    check('card: the provenance footer carries the tier AND the numbers behind it',
      /exact form/.test(txt(card, '.ex-bub-meta')) && /match 6, next 2/.test(txt(card, '.ex-bub-meta')),
      txt(card, '.ex-bub-meta'));
    check('card: "understood as" shows the metric, the cut and the period as chips',
      /SL/.test(txt(card, '.ex-bub-chip')) && /sklad/.test(txt(card, '.ex-bub-chip'))
      && /month 7/.test(txt(card, '.ex-bub-chip')), txt(card, '.ex-bub-chip'));
    check('card: the chips never repeat a VALUE of the answer',
      txt(card, '.ex-bub-chip').indexOf('0.91') < 0, txt(card, '.ex-bub-chip'));

    // "How it was calculated" is closed until it is asked for, and then it is the
    // record's own SQL — not a paraphrase of it.
    const sqlPre = card.querySelector('.ex-bub-sql');
    check('card: the SQL is hidden until "How it was calculated" is pressed', sqlPre.hidden === true);
    click(card.querySelector('[data-chat-act="how"]'));
    check('card: … and then it is the record\'s own SQL, verbatim',
      sqlPre.hidden === false && sqlPre.textContent === 'SELECT sl FROM t', sqlPre.textContent);

    // Chart | Table over the same rows: no second query, no second record.
    const tableWrap = card.querySelector('.ex-bub-tablewrap');
    check('card: the table half is built but hidden behind the segment', tableWrap.hidden === true);
    click(card.querySelector('[data-chat-act="table"]'));
    check('card: "Table" swaps the chart for the rows',
      tableWrap.hidden === false && card.querySelector('.ex-bub-body').hidden === true,
      String(tableWrap.hidden));
    check('card: the table holds the record\'s own rows and nothing else',
      card.querySelectorAll('.ex-bub-table').length === 1
      && /Northport/.test(txt(card, '.ex-bub-table')), txt(card, '.ex-bub-table').slice(0, 80));
    click(card.querySelector('[data-chat-act="chart"]'));
    check('card: "Chart" puts the chart back', tableWrap.hidden === true);

    check('card: the follow-ups are the cuts this metric has and this answer did not use',
      card.querySelectorAll('[data-chat-act="followup"]').length > 0
      && /by client/.test(txt(card, '[data-chat-act="followup"]')), txt(card, '[data-chat-act="followup"]'));

    // 👍/👎 are SVG, not emoji, and the vote is on the RECORD (P4 posts it).
    check('vote: the thumbs are inline SVG, never a font glyph',
      card.querySelectorAll('svg').length >= 2, String(card.querySelectorAll('svg').length));
    click(card.querySelector('[data-chat-act="vote-down"]'));
    await tick(6);
    const votedKey = Array.from(chatBackend.mem.keys()).pop();
    check('vote: 👎 is stored on the record, in this browser',
      chatBackend.mem.get(votedKey).vote === 'down', String(chatBackend.mem.get(votedKey).vote));
    check('vote: … and the chat says what it did with it',
      /incorrect/.test(byIdMap['ex-chat-status'].textContent), byIdMap['ex-chat-status'].textContent);

    // A model-chosen metric and free SQL wear different badges: a reader must
    // be able to tell how much of the answer was a guess.
    push(Object.assign({}, ANS, { question: 'how much was shipped in July?', source: 'model', match: null }));
    await tick(10);
    check('card: a model-chosen metric is badged "understood by meaning"',
      txt(lastBub(), '.ex-bub-tag') === 'understood by meaning', txt(lastBub(), '.ex-bub-tag'));
    push(Object.assign({}, ANS, { question: 'total over all warehouses', kind: 'sql', source: 'semantic_sql', match: null }));
    await tick(10);
    check('card: free SQL over the semantics says so',
      txt(lastBub(), '.ex-bub-tag') === 'SQL from semantics', txt(lastBub(), '.ex-bub-tag'));

    // ── the invariant, rendered: nothing answered -> a question back ──
    push(Object.assign({}, ANS, { question: 'SL for client "Client A" in July?', match: { score: 5, runner_up: 4.6 },
      shortlist: ['orders'],
      intent: { kpi: 'sl', generator_id: 7, dims: [],
                filters: [{ dim: 'month', op: '=', value: 7 }, { dim: 'client', op: '=', value: 'Client A' }] },
      res: { columns: ['value'], rows: [], total: 0 } }));
    await tick(10);
    const empty = lastBub();
    check('empty: an answer with no rows is a clarify card, never a blank one',
      empty.querySelectorAll('.ex-bub-ask').length === 1
      && empty.querySelectorAll('.ex-bub-num').length === 0, txt(empty, '.ex-bub-ask-t'));
    const opts = Array.from(empty.querySelectorAll('.ex-bub-opt')).map(b => b.getAttribute('data-clarify'));
    check('empty: it offers the filters it can drop, the runner-up metric and the composer',
      opts.indexOf('allperiod') >= 0 && opts.indexOf('nofilter') >= 0
      && opts.indexOf('alt') >= 0 && opts[opts.length - 1] === 'ask', opts.join(','));
    check('empty: the coverage of the index is part of the question back',
      /In the index/.test(txt(empty, '.ex-bub-ask-d')) || /What should be calculated/.test(txt(empty, '.ex-bub-ask-d')),
      txt(empty, '.ex-bub-ask-d'));
    check('empty: two metrics within a point -> the card admits the reading was close',
      /two metrics almost equally/.test(txt(empty, '.ex-bub-ask-d')), txt(empty, '.ex-bub-ask-d'));
    const fiN = CALLS.finishIntent.length;
    click(empty.querySelector('[data-clarify="allperiod"]'));
    await tick(8);
    check('empty: "for the whole period" re-runs the SAME intent with the time filter dropped',
      CALLS.finishIntent.length === fiN + 1
      && JSON.stringify(CALLS.finishIntent[fiN].intent.filters) === '[{"dim":"client","op":"=","value":"Client A"}]',
      JSON.stringify(CALLS.finishIntent[fiN] || {}).slice(0, 160));

    // ── textContent, everywhere ──────────────────────────────────────
    const EVIL = '<b onmouseover=alert(1)>Northport</b>';
    push(Object.assign({}, ANS, { question: 'by warehouse', match: null,
      res: { columns: ['sklad', 'value'], rows: [[EVIL, 0.5], ['Eastfield', 0.4]], total: 2 } }));
    await tick(10);
    const evilCard = lastBub();
    check('xss: a row label that came back as markup stays TEXT in the card',
      txt(evilCard, '.ex-bub-bar-label').indexOf(EVIL) >= 0
      && evilCard.querySelector('.ex-bub-bar-label').children.length === 0,
      txt(evilCard, '.ex-bub-bar-label'));
    click(evilCard.querySelector('[data-chat-act="table"]'));
    check('xss: … and in the table half too',
      /onmouseover/.test(txt(evilCard, '.ex-bub-table'))
      && evilCard.querySelector('.ex-bub-table').querySelectorAll('b').length === 0,
      txt(evilCard, '.ex-bub-table').slice(0, 80));
  }

  // ── P3a: the pending card — real stages, a real clock, a real cancel ──
  {
    const realAsk = OP.ask;
    let release = null;
    OP.ask = (q, o) => { CALLS.ask.push({ q, opts: o }); return new Promise(r => { release = () => r(true); }); };
    let cancelled = 0;
    OP.cancelAsk = () => { cancelled++; };
    const recsBefore = chatBackend.mem.size;
    byIdMap['ex-chat-input'].value = 'a long question';
    click(byIdMap['ex-chat-send']);
    await tick(4);
    const pending = T().querySelector('.ex-bub-pending');
    check('pending: the card carries the question and the three real stages',
      pending && /a long question/.test(txt(pending, '.ex-bub-q'))
      && pending.querySelectorAll('.ex-pend-stage').length === 3,
      pending ? String(pending.querySelectorAll('.ex-pend-stage').length) : 'no card');
    check('pending: it starts on "understanding the question"',
      pending.querySelector('[data-stage="intent"]').classList.contains('is-on')
      && /understanding the question/.test(txt(pending, '[data-stage="intent"]')));
    check('pending: the clock runs in tenths of a second',
      /^\d+[.,]\d s$/.test(txt(pending, '.ex-pend-timer')), txt(pending, '.ex-pend-timer'));
    // The stages follow the page's own events. Nothing here is on a timer.
    emitOP('stage', { stage: 'sql' });
    check('pending: a real `stage` event moves it on, and marks the step behind as done',
      pending.querySelector('[data-stage="sql"]').classList.contains('is-on')
      && pending.querySelector('[data-stage="intent"]').classList.contains('is-done')
      && !pending.querySelector('[data-stage="exec"]').classList.contains('is-on'));
    emitOP('stage', { stage: 'exec' });
    check('pending: … and again for the engine',
      pending.querySelector('[data-stage="exec"]').classList.contains('is-on'));
    click(pending.querySelector('[data-chat-act="cancel"]'));
    await tick(4);
    check('cancel: it really aborts the page\'s question, not just the card',
      cancelled === 1, String(cancelled));
    check('cancel: what stands afterwards is a short "Cancelled" card, not blank space',
      /Cancelled/.test(pending.textContent) && /a long question/.test(txt(pending, '.ex-bub-q')),
      pending.textContent.slice(0, 120));
    check('cancel: … which offers the same question again',
      pending.querySelectorAll('[data-chat-act="again"]').length === 1);
    release();
    await tick(12);
    OP.ask = realAsk;
    check('cancel: a cancelled question leaves no record and no "no answer" bubble',
      chatBackend.mem.size === recsBefore, String(chatBackend.mem.size - recsBefore));
    check('cancel: the card is still on screen after the page gave up',
      /Cancelled/.test(T().textContent), '');
    byIdMap['ex-chat-input'].value = '';

    // ── adversarial P3a, major 1: "Cancel" must not eat the NEXT question.
    // The page's own 'cancelled' event is a MESSAGE — answering it with
    // cancelAsk() raised explore.js's sticky flag again, and the follow-up
    // that came after (which never passes through ask()) died in gate 1.
    const cancelledBefore = cancelled;
    emitOP('cancelled', { question: 'a long question' });
    check('cancel: the page\'s own "cancelled" is never answered with cancelAsk()',
      cancelled === cancelledBefore, String(cancelled - cancelledBefore));

    // And the next question really goes through: the in-flight lock is
    // released by the cancel, not held until the aborted request answers.
    const askAfter = CALLS.ask.length;
    OP.state.openAnswer = null;
    byIdMap['ex-chat-input'].value = 'by week?';
    click(byIdMap['ex-chat-send']);
    await tick(10);
    check('cancel: the question asked right after a cancel is sent, not swallowed',
      CALLS.ask.length === askAfter + 1, String(CALLS.ask.length - askAfter));
    check('cancel: … and it lands as a real answer, not another "Cancelled"',
      /by week/.test(T().textContent) && !/Cancelled — the answer was not fully calculated/.test(lastBub().textContent),
      lastBub().textContent.slice(0, 120));
  }

  // ── adversarial P3a, minor 4: one question at a time ─────────────────
  // Enter, the follow-up chips, "Ask again", an intro suggestion and
  // "Regenerate" all reach sendQuestion() without touching the send
  // button — so the button's `disabled` was never the lock it looked like.
  {
    const realAsk = OP.ask;
    let release = null;
    OP.ask = (q, o) => { CALLS.ask.push({ q, opts: o }); return new Promise(r => { release = () => r(true); }); };
    const askN = CALLS.ask.length;
    OP.state.openAnswer = null;
    byIdMap['ex-chat-input'].value = 'first question';
    click(byIdMap['ex-chat-send']);
    click(byIdMap['ex-chat-send']);
    await tick(4);
    check('in-flight: two sends in a row make ONE request, not two',
      CALLS.ask.length === askN + 1, String(CALLS.ask.length - askN));
    check('in-flight: … and ONE pending card',
      T().querySelectorAll('.ex-bub-pending').length === 1,
      String(T().querySelectorAll('.ex-bub-pending').length));
    release();
    await tick(12);
    OP.ask = realAsk;
    byIdMap['ex-chat-input'].value = '';
  }

  // ── adversarial P3a, major 3: no orphan clock ────────────────────────
  // The pending card runs a 100 ms interval. The two "no index" returns
  // are taken BEFORE any answer, and they left it running for the rest of
  // the session — one leaked timer per question asked without an index.
  {
    const realSI = global.setInterval, realCI = global.clearInterval;
    let live = 0;
    global.setInterval = function (fn, ms) { live++; return realSI(fn, ms); };
    global.clearInterval = function (h) { if (h != null) live--; return realCI(h); };
    const indexed = OP.state.indexed;
    OP.state.indexed = false;
    const recsBefore = chatBackend.mem.size;
    byIdMap['ex-chat-input'].value = 'a question with no index';
    click(byIdMap['ex-chat-send']);
    await tick(10);
    OP.state.indexed = indexed;
    global.setInterval = realSI; global.clearInterval = realCI;
    check('no index: the pending card (and its 100 ms clock) is not left behind',
      live === 0 && !T().querySelector('.ex-bub-pending'), 'live=' + live);
    check('no index: the question is still IN the thread — as a failure record',
      chatBackend.mem.size === recsBefore + 1, String(chatBackend.mem.size - recsBefore));
    // The lock the previous block introduced must be released by that
    // return too, or the chat answers exactly one question per page load.
    const askN2 = CALLS.ask.length;
    OP.state.openAnswer = null;
    byIdMap['ex-chat-input'].value = 'a question after the refusal';
    click(byIdMap['ex-chat-send']);
    await tick(10);
    check('no index: the next question is not blocked by the refused one',
      CALLS.ask.length === askN2 + 1, String(CALLS.ask.length - askN2));
    byIdMap['ex-chat-input'].value = '';
  }

  // ── adversarial P3a, major 2: "Regenerate" that failed ─────────
  // The old answer was marked `superseded` BEFORE the new question was
  // asked, and `.ex-bub.is-old .ex-bub-a` is display:none — a regenerate
  // that failed hid the only answer there was.
  {
    const startCount = chatBackend.mem.size;
    answerListeners.slice().forEach(fn => fn({
      question: 'how much for July?', kind: 'kpi', source: 'ask',
      intent: { kpi: 'sl', generator_id: 7, dims: [], filters: [] },
      kpi: { key: 'sl' }, sql: 'SELECT 9',
      res: { columns: ['value'], rows: [[0.81]], total: 1 },
    }));
    await tick(10);
    const target = lastBub();
    const rn = Number(target.getAttribute('data-n'));
    const regenBtn = target.querySelector('[data-chat-act="regen"]');
    // The regenerate fails the way a provider error fails: true from ask(),
    // no 'answer' at all.
    OP.ASK_SILENT = true;
    click(regenBtn);
    await tick(12);
    OP.ASK_SILENT = false;
    const recAfter = Array.from(chatBackend.mem.values()).find(r => Number(r.n) === rn);
    check('regen: a regenerate that produced nothing does NOT mark the old answer',
      recAfter && recAfter.superseded === false, JSON.stringify(recAfter && recAfter.superseded));
    const stillThere = Array.from(T().children).find(c => c.getAttribute && c.getAttribute('data-n') === String(rn));
    check('regen: … so the card that exists stays visible, not hidden as "is-old"',
      stillThere && !stillThere.classList.contains('is-old')
      && stillThere.querySelectorAll('.ex-bub-a').length === 1,
      stillThere ? stillThere.className : 'gone');
    // And the successful case still marks it — once the replacement exists.
    if (OP.state) { OP.state.askRegen = true; OP.state.askParentN = rn; }
    answerListeners.slice().forEach(fn => fn({
      question: 'how much for July?', kind: 'kpi', source: 'ask', regen: true, parent_n: rn,
      intent: { kpi: 'sl', generator_id: 7, dims: [], filters: [] },
      kpi: { key: 'sl' }, sql: 'SELECT 10',
      res: { columns: ['value'], rows: [[0.83]], total: 1 },
    }));
    await tick(12);
    const recDone = Array.from(chatBackend.mem.values()).find(r => Number(r.n) === rn);
    check('regen: a regenerate that ANSWERED marks the answer it replaced',
      recDone && recDone.superseded === true, JSON.stringify(recDone && recDone.superseded));
    check('regen: and the old card becomes the one quiet line',
      /regenerated/.test((Array.from(T().children).find(c => c.getAttribute
        && c.getAttribute('data-n') === String(rn)) || { textContent: '' }).textContent), '');
    check('regen: nothing was lost — the answer, the failed attempt and the new version',
      chatBackend.mem.size === startCount + 3, String(chatBackend.mem.size - startCount));
  }

  // ── P3a: the head — the coverage pill and the "⋯" menu ───────────────
  {
    OP.state.indexStats = { rows: 14294 };
    OP.state.workerTables = [{ partitions: [{ key: 'week 23' }] }];
    click(byIdMap['ex-chat-btn']); await tick(2);
    click(byIdMap['ex-chat-btn']); await tick(6);
    check('coverage: the pill says what the index in THIS browser covers',
      byIdMap['ex-chat-cov'].hidden === false
      && /week 23/.test(byIdMap['ex-chat-cov-text'].textContent)
      && /rows/.test(byIdMap['ex-chat-cov-text'].textContent),
      byIdMap['ex-chat-cov-text'].textContent);
    check('coverage: it costs no request — the last fetch is still the pin POST',
      CALLS.fetch.url === '/api/explore/questions', CALLS.fetch.url);
    click(byIdMap['ex-chat-more']);
    check('menu: «⋯» opens', menuEl.hidden === false);
    // A menu that only closes by picking something is a trap.
    fireDoc('click', { target: byIdMap['ex-chat-thread'] });
    check('menu: a click anywhere outside closes it', menuEl.hidden === true);
    click(byIdMap['ex-chat-more']);
    fireDoc('click', { target: menuEl.children[0] });
    check('menu: … while a click INSIDE it does not', menuEl.hidden === false);
    fireDoc('keydown', { key: 'Escape' });
    check('menu: Escape closes the menu', menuEl.hidden === true);
    click(byIdMap['ex-chat-more']);
    click(menuEl.querySelectorAll('[data-m="new"]')[0]);
    await tick(8);
    check('menu: picking an item closes the menu', menuEl.hidden === true);
    global.confirm = () => true;
    click(menuEl.querySelectorAll('[data-m="clear"]')[0]);
    await tick(8);
    check('menu: "Clear chat" is the control that already exists, not a second one',
      byIdMap['ex-chat-thread'].querySelectorAll('.ex-bub').length === 0,
      String(byIdMap['ex-chat-thread'].querySelectorAll('.ex-bub').length));
    OP.state.indexStats = {}; OP.state.workerTables = [];
  }

  // ── phase 9.4: the folder pill over a SERVER index ────────────────
  // In the viewer's pane there is no folder to connect — the report's own
  // published index answers. The head has to say that and offer NOTHING,
  // because every button it could offer would be a lie.
  {
    const folderEl = byIdMap['ex-chat-folder'];
    const folderBtn = byIdMap['ex-chat-folder-btn'];
    // Re-entering the chat is what resyncs the head (showChat → syncFolderHead).
    const reopen = async () => {
      if (!chatSection.hidden) { click(byIdMap['ex-chat-btn']); await tick(2); }
      click(byIdMap['ex-chat-btn']);
      await tick(4);
    };

    OP.state.source = null; OP.state.dirHandle = null; OP.state.indexed = true;
    await reopen();
    check('chat head: with no index anywhere the pill asks for a RUN, never for a folder',
      /recalculate the report/.test(folderEl.textContent) && folderBtn.hidden === true,
      folderEl.textContent + ' / btn hidden=' + folderBtn.hidden);

    OP.state.source = 'server'; OP.state.serverParts = 34;
    await reopen();
    check('chat head: an index loaded from the server says so, with its partition count',
      folderEl.textContent === 'index from server · 34 partitions', folderEl.textContent);
    check('chat head: … and offers no button — there is nothing left to press',
      folderBtn.hidden === true, 'hidden=' + folderBtn.hidden);
    check('chat head: the pill reads as OK, not as a warning',
      folderEl.className.indexOf('is-ok') >= 0, folderEl.className);

    // A server index that has NOT finished is not a state to brag about.
    OP.state.indexed = false;
    await reopen();
    check('chat head: an unfinished server load does not claim an index',
      folderEl.textContent !== 'index from server · 34 partitions', folderEl.textContent);

    // A connected folder wins: it is fresher than any published run.
    OP.state.source = 'folder'; OP.state.indexed = true;
    OP.state.dirHandle = { name: 'SL' }; OP.state.lastFiles = [{ name: 'a' }, { name: 'b' }];
    await reopen();
    check('chat head: a folder index still names the folder, not the server',
      /folder "SL"/.test(folderEl.textContent) && folderBtn.hidden === true, folderEl.textContent);

    // The engine got dropped (120 s timeout / "Abort"): the index is GONE,
    // not "still building". Prod 2026-08-23: the pill said "indexing…" for
    // ever and every question was refused with "please wait".
    OP.state.indexed = false; OP.state.indexing = false; OP.state.lastKill = 'aborted';
    await reopen();
    check('chat head: a dropped engine says "index reset" and offers to re-index',
      /index reset/.test(folderEl.textContent) && folderBtn.hidden === false
      && folderBtn.textContent === 'Refresh index', folderEl.textContent + ' / ' + folderBtn.textContent);
    byIdMap['ex-chat-input'].value = 'by week?';
    click(byIdMap['ex-chat-send']);
    await tick(2);
    check('chat: asking on a dropped index names the step, not "please wait"',
      /Refresh index/.test(byIdMap['ex-chat-status'].textContent), byIdMap['ex-chat-status'].textContent);
    OP.state.indexing = true; OP.state.lastKill = null;
    await reopen();
    check('chat head: while an index is actually building the pill says so and offers nothing',
      /indexing/.test(folderEl.textContent) && folderBtn.hidden === true, folderEl.textContent);
    // … including while the SERVER index is being downloaded (prod: the pill
    // offered "Load index" during the very load it had started itself).
    OP.state.dirHandle = null; OP.state.source = null;
    OP.state.serverManifests = { '7': { files: [{}, {}] } }; OP.state.reportGen = '7';
    await reopen();
    check('chat head: during the server download the pill shows progress, not a "Load index" button',
      /loading the report index/.test(folderEl.textContent) && folderBtn.hidden === true,
      folderEl.textContent + ' / hidden=' + folderBtn.hidden);
    OP.state.dirHandle = { name: 'SL' }; OP.state.source = 'folder'; OP.state.serverManifests = {};
    // A question asked WHILE the index builds waits and goes by itself (prod
    // 2026-08-23: "The index is still building — please wait" was a dead end).
    OP.whenIndexed = () => new Promise(r => setTimeout(() => { OP.state.indexing = false; OP.state.indexed = true; r(true); }, 20));
    byIdMap['ex-chat-input'].value = 'by week?';
    click(byIdMap['ex-chat-send']);
    await tick(1);
    check('chat: a question during the build is queued, not refused',
      /will be sent automatically/.test(byIdMap['ex-chat-status'].textContent), byIdMap['ex-chat-status'].textContent);
    await new Promise(r => setTimeout(r, 60));
    check('chat: … and it went once the index was ready (status no longer says "is building")',
      !/is building/.test(byIdMap['ex-chat-status'].textContent), byIdMap['ex-chat-status'].textContent);
    delete OP.whenIndexed;
    OP.state.indexing = false; OP.state.indexed = true;
  }

  // ── P4: the vote leaves the browser, and takes nothing with it ──────
  // Deliberately last in this section: it is the only thing here that makes
  // a request, and the coverage pill above pins «no request at all».
  {
    const push = (ev) => answerListeners.slice().forEach(fn => fn(ev));
    const before = CALLS.fetch && CALLS.fetch.url;
    push({ question: 'what was SL in July?', kind: 'kpi', source: 'form',
           intent: { kpi: 'sl', generator_id: 7, dims: ['sklad'], filters: [] },
           kpi: { key: 'sl', short: 'SL' }, sql: 'SELECT sl FROM t',
           res: { columns: ['sklad', 'value'], rows: [['Northport', 0.91]], total: 1 } });
    await tick(10);
    const noIdCard = lastBub();
    click(noIdCard.querySelector('[data-chat-act="vote-up"]'));
    await tick(6);
    check('vote: an answer with no server id posts NOTHING (the vote stays local)',
      (CALLS.fetch && CALLS.fetch.url) === before, String(CALLS.fetch && CALLS.fetch.url));

    // A different question on purpose: P3b's dedup would drop a second
    // "answer" for the same words inside its 1.5 s window.
    push({ question: 'what was SL in June?', kind: 'kpi', source: 'form', answer_id: 412,
           intent: { kpi: 'sl', generator_id: 7, dims: ['sklad'], filters: [] },
           kpi: { key: 'sl', short: 'SL' }, sql: 'SELECT sl FROM t',
           res: { columns: ['sklad', 'value'], rows: [['Northport', 0.91]], total: 1 } });
    await tick(10);
    const card4 = lastBub();
    click(card4.querySelector('[data-chat-act="vote-up"]'));
    await tick(8);
    check('vote: … and with one it goes to that answer\'s own vote route',
      CALLS.fetch.url === '/api/explore/answers/412/vote'
      && CALLS.fetch.opts.method === 'POST', CALLS.fetch.url);
    check('vote: OriginGuard\'s two conditions — same-origin credentials, JSON type',
      CALLS.fetch.opts.credentials === 'same-origin'
      && CALLS.fetch.opts.headers['Content-Type'] === 'application/json',
      JSON.stringify(CALLS.fetch.opts.headers) + ' / ' + CALLS.fetch.opts.credentials);
    check('vote: the body is the verdict and nothing else — no SQL, no question, no row',
      JSON.stringify(JSON.parse(CALLS.fetch.opts.body)) === '{"vote":"up"}', CALLS.fetch.opts.body);
    click(card4.querySelector('[data-chat-act="vote-up"]'));
    await tick(8);
    check('vote: pressing the same thumb again sends the removal, not a second vote',
      JSON.parse(CALLS.fetch.opts.body).vote === null, CALLS.fetch.opts.body);
  }

  // ═════════════════════════════════════════════════════════════════
  // P3b — "Refine", idempotency, the "⋯" menu
  // ═════════════════════════════════════════════════════════════════
  const push = (ev) => answerListeners.slice().forEach(fn => fn(ev));
  const ANSWER = (over) => Object.assign({
    question: 'what was SL in July?', kind: 'kpi', source: 'form',
    intent: { kpi: 'sl', generator_id: 7, dims: [], filters: [{ dim: 'month', op: '=', value: 7 }] },
    kpi: { key: 'sl', short: 'SL' }, sql: 'SELECT sl FROM t',
    res: { columns: ['value'], rows: [[0.87]], total: 1 },
  }, over || {});

  // A clean conversation to work in: "New conversation" is itself under test
  // below, so this block starts from the empty thread "Clear chat" leaves.
  global.confirm = () => true;
  click(byIdMap['ex-chat-clear']);
  await tick(10);

  // ── overlapping renders paint the empty state ONCE ───────────────────
  // The first paint and the re-render after the index arrives used to race:
  // both cleared the thread, both appended the intro (stand 2026-09-07).
  {
    const api = global.window.__opChatApi;
    api.renderThread(); api.renderThread(); api.renderThread();
    await tick(12);
    const intros = byIdMap['ex-chat-thread'].querySelectorAll('.ex-chat-intro').length;
    check('intro: three overlapping renderThread() calls leave exactly one empty state', intros === 1, String(intros));
  }

  // ── the intro's coverage line follows the index, like the pill ───────
  // It was painted once (before the index arrived) and never redrawn, so the
  // pane said "the index is still loading…" under a 36-partition pill.
  {
    const api = global.window.__opChatApi;
    const saved = { indexStats: OP.state.indexStats, workerTables: OP.state.workerTables };
    OP.state.indexStats = null; OP.state.workerTables = [];
    await api.renderThread(); await tick(12);
    const line = () => byIdMap['ex-chat-thread'].querySelector('.ex-chat-intro').querySelector('.ex-chat-covline');
    check('intro covline: no index yet -> not claiming coverage', line() && !/in the index/.test(line().textContent), line() && line().textContent);
    OP.state.indexStats = { rows: 208220 }; OP.state.workerTables = [{ partitions: [{ key: '23' }, { key: '24' }] }];
    api.syncCoverage();
    check('intro covline: syncCoverage() rewrites it once the index is there', line() && /in the index/.test(line().textContent), line() && line().textContent);
    OP.state.indexStats = saved.indexStats; OP.state.workerTables = saved.workerTables;
  }

  // ── idempotency: two 'answer' events for ONE question ────────────────
  {
    const before = chatBackend.mem.size;
    const ev = ANSWER({ question: 'a question with two events' });
    push(ev);
    push(ev);                       // the follow-up branch AND the main path
    await tick(12);
    check('dedup: two «answer» events for one question make ONE record',
      chatBackend.mem.size === before + 1, String(chatBackend.mem.size - before));
    check('dedup: … so the counter the pane shows is not inflated either',
      byIdMap['ex-chat-count'].textContent === '1', byIdMap['ex-chat-count'].textContent);
    // A deliberate re-run is not a duplicate: it must always land.
    const n0 = chatBackend.mem.size;
    push(ANSWER({ question: 'a question with two events', regen: true, sql: 'SELECT 2' }));
    push(ANSWER({ question: 'a question with two events', regen: true, sql: 'SELECT 3' }));
    await tick(12);
    check('dedup: "Regenerate" bypasses it — both re-runs are recorded',
      chatBackend.mem.size === n0 + 2, String(chatBackend.mem.size - n0));
  }

  // ── idempotency: the same question sent twice while it is in flight ──
  // Adversarial P3b, major 5: counting requests does NOT test the `inflight`
  // map — the `inFlight` boolean one line below swallows the second send just
  // as well, and `if (already)` could be `if (false)` with every assertion
  // still green. The map's whole promise is that the second caller gets the
  // FIRST answer, so the test has to hold the promises and compare them.
  {
    const realAsk = OP.ask;
    let release = null;
    OP.ask = (q, o) => { CALLS.ask.push({ q, opts: o }); return new Promise(r => { release = () => r(true); }); };
    const askN = CALLS.ask.length;
    OP.state.openAnswer = null;
    const api = global.window.__opChatApi;
    check('in-flight: the page exposes sendQuestion to the harness (localhost only)',
      !!(api && typeof api.sendQuestion === 'function'));
    const p1 = api.sendQuestion('one and the same question');
    const p2 = api.sendQuestion('one and the same question');
    const p3 = api.sendQuestion('one and the same question');
    await tick(4);
    check('in-flight: the second and third callers get the FIRST promise back',
      p1 === p2 && p2 === p3, String(p1 === p2) + '/' + String(p2 === p3));
    check('in-flight: three sends of the SAME question make one request',
      CALLS.ask.length === askN + 1, String(CALLS.ask.length - askN));
    check('in-flight: … and one pending card',
      T().querySelectorAll('.ex-bub-pending').length === 1,
      String(T().querySelectorAll('.ex-bub-pending').length));
    release();
    const [v1, v2, v3] = await Promise.all([p1, p2, p3]);
    check('in-flight: … and all three callers see the SAME answer, not a refusal',
      v1 === v2 && v2 === v3 && v1 !== false, JSON.stringify([v1, v2, v3]));
    await tick(12);
    // A DIFFERENT question, asked while that one was in flight, is refused —
    // that is the `inFlight` boolean, and it is a different rule.
    OP.ask = realAsk;
    byIdMap['ex-chat-input'].value = '';
  }

  // ── adversarial P3b, minor 11: "Regenerate" keeps the pin ──────
  // "Refine" pins the metric; the re-run used to go back without it, and
  // the router was free to walk straight back to the metric the reader had
  // just rejected.
  {
    click(byIdMap['ex-chat-clear']); await tick(10);
    const api = global.window.__opChatApi;
    OP.state.openAnswer = null;
    // (a question of its own: the 1.5 s answer-dedup window is shared;
    // Russian on purpose — it matches the fixture's Russian question forms)
    await api.sendQuestion('какой объём отгрузок за апрель?', { fixedKpi: 'ship' });
    await tick(10);
    const rec = Array.from(chatBackend.mem.values()).pop();
    check('regen: the pinned metric is part of the record',
      rec.fixed_kpi === 'ship', JSON.stringify(rec.fixed_kpi));
    const askN = CALLS.ask.length;
    click(lastBub().querySelector('[data-chat-act="regen"]'));
    await tick(12);
    check('regen: … and the re-run carries it back to the router',
      CALLS.ask.length === askN + 1
      && (CALLS.ask[askN].opts || {}).fixedKpi === 'ship',
      JSON.stringify((CALLS.ask[askN] || {}).opts || {}));
    click(byIdMap['ex-chat-clear']); await tick(10);
  }

  // ── "Refine": the parent is THIS card, not the newest one ─
  // Deliberate Cyrillic below: Russian questions scored against the Russian
  // KPI forms (shortlistAlts) and a Russian follow-up (matchFollowUp).─────────
  {
    click(byIdMap['ex-chat-clear']); await tick(10);
    push(ANSWER({ question: 'what was SL in July?' }));
    await tick(10);
    push(ANSWER({ question: 'какой объём отгрузок за июль?',
      intent: { kpi: 'ship', generator_id: 7, dims: [], filters: [] },
      kpi: { key: 'ship', short: 'Shipments' } }));
    await tick(10);
    // The page's OPEN answer is the newest one — the clarification must
    // ignore it and take the card it was opened on.
    OP.state.openAnswer = { question: 'какой объём отгрузок за июль?',
      intent: { kpi: 'ship', generator_id: 7, dims: [], filters: [] } };
    const cards = bubbles();
    check('refine: every answer card offers "Refine"',
      cards.length === 2 && cards.every(c => c.querySelectorAll('[data-chat-act="refine"]').length === 1),
      String(cards.length));
    const first = cards[0];
    check('refine: the panel is not built until it is asked for',
      first.querySelectorAll('[data-chat-act="refine-text"]').length === 0);
    click(first.querySelector('[data-chat-act="refine"]'));
    check('refine: … and then the free-text field comes FIRST',
      first.querySelectorAll('[data-chat-act="refine-text"]').length === 1
      && first.querySelectorAll('[data-chat-act="refine-send"]').length === 1);
    const fiN = CALLS.finishIntent.length, askN = CALLS.ask.length;
    const box = first.querySelector('[data-chat-act="refine-text"]');
    box.value = 'а по неделям?';
    click(first.querySelector('[data-chat-act="refine-send"]'));
    await tick(12);
    check('refine: a clarification runs against the intent of THIS card (SL has a week cut, "Shipments" has none)',
      CALLS.finishIntent.length === fiN + 1 && CALLS.ask.length === askN
      && CALLS.finishIntent[fiN].intent.kpi === 'sl'
      && JSON.stringify(CALLS.finishIntent[fiN].intent.dims) === '["week"]',
      JSON.stringify((CALLS.finishIntent[fiN] || {}).intent || {}));
    check('refine: … and hangs off that card, not off the newest answer',
      OP.state.askParentN === 1, String(OP.state.askParentN));
  }

  // ── "Refine": an alternative from the shortlist rides as fixed_kpi ──
  {
    click(byIdMap['ex-chat-clear']); await tick(10);
    push(ANSWER({ question: 'какой объём отгрузок за июль?', shortlist: ['sl', 'ship'] }));
    await tick(10);
    const card = lastBub();
    click(card.querySelector('[data-chat-act="refine"]'));
    const altBtns = card.querySelectorAll('[data-chat-act="refine-alt"]');
    check('refine: the alternatives are the shortlist, minus the metric that answered',
      altBtns.length === 1 && altBtns[0].getAttribute('data-kpi') === 'ship',
      altBtns.map(b => b.getAttribute('data-kpi')).join(','));
    check('refine: each alternative says how well it fits, in per cent',
      /match \d+%/.test(altBtns[0].textContent), altBtns[0].textContent);
    const askN = CALLS.ask.length;
    click(altBtns[0]);
    await tick(12);
    const last = CALLS.ask[CALLS.ask.length - 1];
    check('refine: clicking one re-asks the SAME question with that metric pinned',
      CALLS.ask.length === askN + 1 && last.q === 'какой объём отгрузок за июль?'
      && (last.opts || {}).fixedKpi === 'ship', JSON.stringify(last || {}));
    check('refine: … and it is still a child of the card it came from',
      OP.state.askParentN === 1, String(OP.state.askParentN));

    // "ask again without this metric" — the best remaining candidate.
    // (The answer above rebuilt the thread, so the panel is opened again.)
    const card2 = bubbles()[0];
    click(card2.querySelector('[data-chat-act="refine"]'));
    const notBtn = card2.querySelector('[data-chat-act="refine-not"]');
    const askN2 = CALLS.ask.length;
    click(notBtn);
    await tick(12);
    check('refine: "without this metric" asks again with another metric pinned',
      CALLS.ask.length === askN2 + 1
      && (CALLS.ask[CALLS.ask.length - 1].opts || {}).fixedKpi === 'ship',
      JSON.stringify(CALLS.ask[CALLS.ask.length - 1] || {}));
    // And the cut/period half of the panel is the clarify options, reused.
    check('refine: the period this answer carried can be dropped from the panel',
      card2.querySelectorAll('[data-chat-act="refine-fix"]').length >= 1,
      String(card2.querySelectorAll('[data-chat-act="refine-fix"]').length));
  }

  // ── clarify: low confidence, coverage, an impossible cut ─────────────
  {
    click(byIdMap['ex-chat-clear']); await tick(10);
    // 1. A model that chose this metric out of a menu that held another one
    //    (adversarial P3b, major 2 — a form match below the threshold is a
    //    record `nl2sql.match_forms` never sends). The number IS shown, and
    //    under it the card admits the reading was a choice.
    // Russian on purpose: shortlistAlts scores it against the Russian KPI forms.
    push(ANSWER({ question: 'объём отгрузок за август?', source: 'model',
      match: null, shortlist: ['sl', 'ship'] }));
    await tick(10);
    const low = lastBub();
    check('clarify: a model-chosen metric keeps the number and asks the question back',
      low.querySelectorAll('.ex-bub-lowconf').length === 1
      && low.querySelectorAll('.ex-bub-num').length === 1, txt(low, '.ex-bub-lowconf'));
    check('clarify: … with the shortlist as the alternatives',
      low.querySelectorAll('[data-chat-act="refine-alt"]').length === 1,
      String(low.querySelectorAll('[data-chat-act="refine-alt"]').length));

    // 2. H10 — zero rows for a period the loaded index does not cover.
    OP.state.workerTables = [{ partitions: [{ key: '2026-06' }] }];
    OP.state.indexStats = { rows: 100 };
    push(ANSWER({ question: 'what was SL in July?',
      intent: { kpi: 'sl', generator_id: 7, dims: [],
                filters: [{ dim: 'month', op: '=', value: 7 }] },
      res: { columns: ['value'], rows: [], total: 0 } }));
    await tick(10);
    const cov = lastBub();
    check('clarify: an empty answer over an uncovered period says so, with the coverage',
      cov.querySelectorAll('.ex-bub-ask-cov').length === 1
      && /2026-06/.test(txt(cov, '.ex-bub-ask-cov')), txt(cov, '.ex-bub-ask-cov'));
    check('clarify: … and offers to drop the period filter',
      cov.querySelectorAll('[data-clarify="allperiod"]').length === 1);

    // 3. A cut the metric does not have.
    push(ANSWER({ question: 'SL by hub?',
      intent: { kpi: 'sl', generator_id: 7, dims: ['hub'], filters: [] },
      res: { columns: ['hub', 'value'], rows: [], total: 0 } }));
    await tick(10);
    const dim = lastBub();
    check('clarify: an impossible cut is named, not silently empty',
      dim.querySelectorAll('.ex-bub-ask-dim').length === 1
      && /can't be sliced by "hub"/.test(txt(dim, '.ex-bub-ask-dim')), txt(dim, '.ex-bub-ask-dim'));
    check('clarify: … and "can\'t be sliced" is said about the CUT only',
      dim.querySelectorAll('.ex-bub-ask-per').length === 0, txt(dim, '.ex-bub-ask-dim'));

    // 3b. adversarial P3b, nit 12: a PERIOD the metric has no cut by. The
    //     question filtered by it and the engine dropped the filter — "can't be
    //     sliced by week. Available: …" answered a question nobody asked.
    push(ANSWER({ question: 'shipment volume in week 27?',
      intent: { kpi: 'ship', generator_id: 7, dims: [],
                filters: [{ dim: 'week', op: '=', value: 27 }] },
      kpi: { key: 'ship', short: 'Shipments' },
      res: { columns: ['value'], rows: [], total: 0 } }));
    await tick(10);
    const per = lastBub();
    check('clarify: a period the metric has no cut by is its own sentence, not "can\'t be sliced"',
      per.querySelectorAll('.ex-bub-ask-per').length === 1
      && /has no cut for period "week"/.test(txt(per, '.ex-bub-ask-per'))
      && per.querySelectorAll('.ex-bub-ask-dim').length === 0,
      txt(per, '.ex-bub-ask-per') + ' | ' + txt(per, '.ex-bub-ask-dim'));

    const chips = dim.querySelectorAll('[data-clarify="usedim"]');
    check('clarify: … and the cuts the metric DOES have are offered as chips',
      chips.length >= 1 && chips.every(c => ['client', 'sklad', 'month', 'year', 'week']
        .indexOf(c.getAttribute('data-dim')) >= 0),
      chips.map(c => c.getAttribute('data-dim')).join(','));
    const fiN = CALLS.finishIntent.length;
    click(chips[0]);
    await tick(10);
    check('clarify: a chip re-runs the same metric by a cut it really has',
      CALLS.finishIntent.length === fiN + 1
      && JSON.stringify(CALLS.finishIntent[fiN].intent.dims)
         === JSON.stringify([chips[0].getAttribute('data-dim')]),
      JSON.stringify((CALLS.finishIntent[fiN] || {}).intent || {}));
    OP.state.workerTables = []; OP.state.indexStats = {};
  }

  // ── the "⋯" menu: a new conversation, the history, the transcript ────
  {
    click(byIdMap['ex-chat-clear']); await tick(10);
    push(ANSWER({ question: 'first conversation, first question' }));
    await tick(10);
    const kept = chatBackend.mem.size;
    check('menu: the conversation to be kept has a record', bubbles().length === 1);
    click(byIdMap['ex-chat-more']);
    click(menuEl.querySelectorAll('[data-m="new"]')[0]);
    await tick(10);
    check('new conversation: the thread on screen is empty and teaches',
      bubbles().length === 0 && T().querySelectorAll('.ex-chat-sugg').length === 1,
      String(bubbles().length));
    check('new conversation: nothing was deleted — the old thread is still stored',
      chatBackend.mem.size === kept, String(chatBackend.mem.size - kept));
    check('new conversation: … and the chat says where the old one went',
      /The previous one is saved/.test(byIdMap['ex-chat-status'].textContent),
      byIdMap['ex-chat-status'].textContent);
    push(ANSWER({ question: 'second conversation, first question' }));
    await tick(10);
    check('new conversation: the next answer lands in the NEW thread only',
      bubbles().length === 1 && /second conversation/.test(txt(lastBub(), '.ex-bub-q')),
      txt(lastBub(), '.ex-bub-q'));

    // adversarial P3b, minor 10: the side list showed the report title twice
    // with the same (newest) question under both — two rows naming neither.
    // (the side list is rebuilt when the pane is opened — toggle it)
    click(byIdMap['ex-chat-btn']); await tick(4); click(byIdMap['ex-chat-btn']); await tick(8);
    const trows = byIdMap['ex-chat-threads'].querySelectorAll('.ex-chat-thread-row');
    const tsubs = trows.map(r => (r.querySelector('.ex-chat-thread-last') || {}).textContent || '');
    check('threads: two conversations of one report are told apart by their FIRST question',
      trows.length >= 2 && tsubs.indexOf('first conversation, first question') >= 0
      && tsubs.indexOf('second conversation, first question') >= 0, tsubs.join(' | '));

    // History: both conversations of THIS report, by their first question.
    click(byIdMap['ex-chat-more']);
    click(menuEl.querySelectorAll('[data-m="hist"]')[0]);
    await tick(10);
    const rows = T().querySelectorAll('.ex-chat-hist-row');
    check('history: every conversation of this report is listed, the new one included',
      rows.length >= 2 && rows.filter(r => r.classList.contains('active')).length === 1,
      String(rows.length));
    check('history: each row is named by the question the conversation STARTED with',
      /first conversation, first question/.test(rows.map(r => r.textContent).join(' ')),
      rows.map(r => r.textContent).join(' | ').slice(0, 160));
    const old = rows.filter(r => /first conversation/.test(r.textContent))[0];
    click(old);
    await tick(10);
    check('history: picking one switches to it',
      bubbles().length === 1 && /first conversation/.test(txt(lastBub(), '.ex-bub-q')),
      txt(lastBub(), '.ex-bub-q'));

    // Copy: the questions and the numbers, never the SQL.
    const COPIED = [];
    // node 24 exposes `navigator` as a getter — it has to be redefined, not assigned.
    Object.defineProperty(global, 'navigator',
      { value: { clipboard: { writeText: async (s) => { COPIED.push(s); } } }, configurable: true });
    click(byIdMap['ex-chat-more']);
    click(menuEl.querySelectorAll('[data-m="copy"]')[0]);
    await tick(10);
    check('copy: the whole conversation went to the clipboard',
      COPIED.length === 1 && /Q: first conversation, first question/.test(COPIED[0])
      && /A: /.test(COPIED[0]), (COPIED[0] || '').slice(0, 160));
    check('copy: and NOT one line of SQL went with it',
      COPIED[0].indexOf('SELECT') < 0, COPIED[0]);
    check('copy: the chat says what it copied',
      /copied/.test(byIdMap['ex-chat-status'].textContent),
      byIdMap['ex-chat-status'].textContent);
  }
}

// ═══════════════════════════════════════════════════════════════════
// Part 3 — the SAME file inside the viewer's pane (`?embed=1`)
// ═══════════════════════════════════════════════════════════════════
// EMBED is decided once, at load time, off `#ex-root`'s data-attributes, so
// the pane's two behaviours — "Show in report" and the width the parent
// posts down — need a second instance over a fresh document. Everything the
// pane sends up is captured; nothing but scalars may be in it.
async function embedCases() {
  const map2 = {};
  const body2 = new El('div');
  const mk2 = (tag, id) => { const e = new El(tag); if (id) { e.attrs.id = id; map2[id] = e; } body2.appendChild(e); return e; };
  ['ex-reports', 'ex-ask', 'ex-answer', 'ex-rows'].forEach(id => mk2('section', id));
  const chat2 = mk2('section', 'ex-chat'); chat2.hidden = true;
  ['ex-chat-btn', 'ex-chat-count', 'ex-chat-threads', 'ex-chat-thread', 'ex-chat-input',
   'ex-chat-send', 'ex-chat-clear', 'ex-chat-chips', 'ex-chat-status', 'ex-chat-folder',
   'ex-chat-folder-btn', 'ex-chat-nav', 'ex-chat-back', 'ex-chat-prev', 'ex-chat-next',
   'ex-chat-recalc', 'ex-chat-cov', 'ex-chat-cov-text', 'ex-chat-more', 'ex-chat-menu',
   'ex-chat-wide', 'ex-chat-close'].forEach(id => mk2('div', id));
  const root2 = mk2('div', 'ex-root');
  root2.dataset.embed = '1'; root2.dataset.report = ''; root2.dataset.mode = '';
  const backend2 = fakeBackend();
  const POSTED = [];
  const win2 = {};
  global.document = {
    createElement: t => new El(t), getElementById: id => map2[id] || null,
    addEventListener: () => {}, querySelectorAll: sel => body2.querySelectorAll(sel),
    querySelector: sel => body2.querySelector(sel),
    createTextNode: v => ({ nodeType: 3, text: String(v) }), body: body2,
  };
  global.window = { OP_EXPLORE: OP, __opChatBackend: backend2,
    // The "Show in report" timeout, shortened for the harness (2 s live).
    __opChatLocTimeout: 20,
    addEventListener: (t, fn) => { (win2[t] = win2[t] || []).push(fn); },
    parent: { postMessage: (m) => POSTED.push(m) } };
  global.location = { hostname: 'localhost', hash: '', pathname: '/explore',
    search: '', origin: 'http://localhost' };
  (0, eval)(fs.readFileSync(path.join(ROOT, 'static', 'js', 'explore-chat.js'), 'utf8'));
  const post = (data) => (win2.message || []).forEach(fn => fn({ origin: 'http://localhost', data: data }));

  click(map2['ex-chat-btn']);
  await tick(6);
  answerListeners.slice().forEach(fn => fn({
    question: 'SL by warehouse?', kind: 'kpi', source: 'form',
    intent: { kpi: 'sl', generator_id: 7, dims: ['sklad'], filters: [] },
    kpi: { key: 'sl', short: 'SL' }, sql: 'SELECT 1',
    res: { columns: ['sklad', 'value'], rows: [['Northport', 0.91]], total: 1 },
  }));
  await tick(12);
  const bub2 = map2['ex-chat-thread'].querySelector('.ex-bub[data-n]');
  const locBtn = bub2 && bub2.querySelector('[data-chat-act="locate"]');
  check('pane: "Show in report" is offered inside the pane', !!locBtn);
  click(locBtn);
  const req = POSTED.filter(m => m.type === 'op_chat_locate').pop();
  check('pane: the request carries an id and the labels to look for',
    !!req && !!req.id && req.labels.length === 1, JSON.stringify(req || {}).slice(0, 160));
  check('pane: the button says it is working and cannot be pressed twice',
    locBtn.disabled === true && /searching the report/.test(bub2.querySelector('.ex-bub-locmsg').textContent),
    bub2.querySelector('.ex-bub-locmsg').textContent);
  post({ type: 'op_chat_located', id: req.id, ok: false, reason: 'nomatch' });
  check('pane: ok:false shows the reason — the button never pretends it scrolled',
    /not found in the report/.test(bub2.querySelector('.ex-bub-locmsg').textContent)
    && locBtn.disabled === false, bub2.querySelector('.ex-bub-locmsg').textContent);
  click(locBtn);
  const req2 = POSTED.filter(m => m.type === 'op_chat_locate').pop();
  post({ type: 'op_chat_located', id: req2.id, ok: false, reason: 'timeout' });
  check('pane: a report that never answered is a DIFFERENT line, not "not found"',
    /did not answer/.test(bub2.querySelector('.ex-bub-locmsg').textContent),
    bub2.querySelector('.ex-bub-locmsg').textContent);
  click(locBtn);
  post({ type: 'op_chat_located', id: POSTED.filter(m => m.type === 'op_chat_locate').pop().id, ok: true });
  check('pane: a hit leaves no message at all — the report itself is the answer',
    bub2.querySelector('.ex-bub-locmsg').textContent === '',
    bub2.querySelector('.ex-bub-locmsg').textContent);
  // adversarial P3a, minor 8: a parent that never answers. No message ever
  // arrives, so nothing but the chat's own clock can end the wait — without
  // it the button stayed disabled and the line said "searching…" forever.
  click(locBtn);
  check('pane: while it waits, the button is held',
    locBtn.disabled === true && /searching the report/.test(bub2.querySelector('.ex-bub-locmsg').textContent),
    bub2.querySelector('.ex-bub-locmsg').textContent);
  await new Promise(r => setTimeout(r, 60));
  check('pane: a parent that never answers times out into the honest line',
    /did not answer/.test(bub2.querySelector('.ex-bub-locmsg').textContent)
    && locBtn.disabled === false, bub2.querySelector('.ex-bub-locmsg').textContent);
  // …and a LATE answer to that request changes nothing (it was settled).
  const late = POSTED.filter(m => m.type === 'op_chat_locate').pop();
  post({ type: 'op_chat_located', id: late.id, ok: true });
  check('pane: a late answer to a timed-out request is ignored',
    /did not answer/.test(bub2.querySelector('.ex-bub-locmsg').textContent),
    bub2.querySelector('.ex-bub-locmsg').textContent);

  // The width the parent posts down: a shorter placeholder, a folded chrome.
  post({ type: 'op_chat_width', w: 360, wide: false });
  check('pane: under 400 px the placeholder gets short and the chrome folds',
    chat2.classList.contains('is-narrow') && map2['ex-chat-input'].placeholder === 'Question about the report…',
    map2['ex-chat-input'].placeholder + ' / ' + chat2.className);
  post({ type: 'op_chat_width', w: 760, wide: true });
  check('pane: a wide pane is neither narrow nor short-spoken',
    !chat2.classList.contains('is-narrow') && chat2.classList.contains('is-wide')
    && map2['ex-chat-input'].placeholder === 'Ask about report figures…',
    map2['ex-chat-input'].placeholder + ' / ' + chat2.className);
  click(map2['ex-chat-wide']);
  check('pane: the "wider" button asks the PARENT to resize (the pane owns the width)',
    (POSTED.filter(m => m.type === 'op_chat_wide').pop() || {}).wide === false,
    JSON.stringify(POSTED.filter(m => m.type === 'op_chat_wide')));
  click(map2['ex-chat-close']);
  check('pane: "Close" asks the parent to close the pane',
    POSTED.filter(m => m.type === 'op_chat_close').length === 1);
  // What may cross the frame is the locate protocol's own fields (the labels
  // the report is asked to find) and a count. Never the SQL, and never a
  // whole result — the thread stays in this browser.
  check('pane: no SQL and no result ever crossed the frame',
    POSTED.every(m => !/SELECT/.test(JSON.stringify(m)))
    && POSTED.every(m => m.type !== 'op_chat_locate' || m.labels.length <= 8),
    JSON.stringify(POSTED).slice(0, 200));
  check('pane: the count that goes up is a bare number',
    POSTED.filter(m => m.type === 'op_chat_count').every(m => typeof m.n === 'number'
      && Object.keys(m).length === 2), JSON.stringify(POSTED.filter(m => m.type === 'op_chat_count')));
}

// ═══════════════════════════════════════════════════════════════════
// Part 4 — a RELOAD: the page comes up over a store that already holds
// two conversations of one report (adversarial P3b, minor 9 and 10)
// ═══════════════════════════════════════════════════════════════════
// `convByBase` is memory, so F5 used to drop the reader into the FIRST
// conversation — and the next question landed in the thread "New
// conversation" had closed. And the badge the pane posts up counted the OPEN
// conversation, so a new one read as "0 questions" in the viewer.
async function reloadCases() {
  const map3 = {};
  const body3 = new El('div');
  const mk3 = (tag, id) => { const e = new El(tag); if (id) { e.attrs.id = id; map3[id] = e; } body3.appendChild(e); return e; };
  ['ex-reports', 'ex-ask', 'ex-answer', 'ex-rows'].forEach(id => mk3('section', id));
  const chat3 = mk3('section', 'ex-chat'); chat3.hidden = true;
  ['ex-chat-btn', 'ex-chat-count', 'ex-chat-threads', 'ex-chat-thread', 'ex-chat-input',
   'ex-chat-send', 'ex-chat-clear', 'ex-chat-chips', 'ex-chat-status', 'ex-chat-folder',
   'ex-chat-folder-btn', 'ex-chat-nav', 'ex-chat-back', 'ex-chat-prev', 'ex-chat-next',
   'ex-chat-recalc', 'ex-chat-cov', 'ex-chat-cov-text', 'ex-chat-more', 'ex-chat-menu',
   'ex-chat-wide', 'ex-chat-close'].forEach(id => mk3('div', id));
  const root3 = mk3('div', 'ex-root');
  root3.dataset.embed = '1'; root3.dataset.report = ''; root3.dataset.mode = '';
  // The store as the previous session left it: an old conversation of report
  // 7 and a newer one started by "New conversation".
  const backend3 = fakeBackend();
  const seed = (rid, n, ts, question) => {
    const rec = CHAT.shapeRecord({ user: 'u1', report_id: rid, n: n, ts: ts, question: question,
      kind: 'kpi', source: 'form', intent: { kpi: 'sl', generator_id: 7 },
      res: { columns: ['value'], rows: [[1]], total: 1 } });
    backend3.mem.set(CHAT.chatKey('u1', rid, n), rec);
  };
  seed('7', 1, 1000, 'old conversation, first question');
  seed('7', 2, 2000, 'old conversation, second question');
  seed('7#c77', 1, 3000, 'new conversation, first question');
  const POSTED3 = [];
  const win3 = {};
  global.document = {
    createElement: t => new El(t), getElementById: id => map3[id] || null,
    addEventListener: () => {}, querySelectorAll: sel => body3.querySelectorAll(sel),
    querySelector: sel => body3.querySelector(sel),
    createTextNode: v => ({ nodeType: 3, text: String(v) }), body: body3,
  };
  global.window = { OP_EXPLORE: OP, __opChatBackend: backend3, __opChatLocTimeout: 20,
    addEventListener: (t, fn) => { (win3[t] = win3[t] || []).push(fn); },
    parent: { postMessage: (m) => POSTED3.push(m) } };
  global.location = { hostname: 'localhost', hash: '', pathname: '/explore',
    search: '', origin: 'http://localhost' };
  OP.state.openAnswer = null; OP.state.reportGen = 7;
  (0, eval)(fs.readFileSync(path.join(ROOT, 'static', 'js', 'explore-chat.js'), 'utf8'));
  await tick(10);
  const api3 = global.window.__opChatApi;
  check('reload: the conversation that comes up is the NEWEST one of this report',
    api3.currentReport() === '7#c77', String(api3.currentReport()));
  click(map3['ex-chat-btn']);
  await tick(10);
  const shown = map3['ex-chat-thread'].querySelectorAll('.ex-bub[data-n]');
  check('reload: … and the thread on screen is that conversation, not the first one',
    shown.length === 1 && /new conversation/.test(shown[0].textContent),
    String(shown.length) + ' / ' + (shown[0] || {}).textContent);
  check('reload: the pane\'s own header still counts the open conversation',
    map3['ex-chat-count'].textContent === '1', map3['ex-chat-count'].textContent);
  const counts = POSTED3.filter(m => m.type === 'op_chat_count').map(m => m.n);
  check('reload: the badge the viewer shows counts the REPORT — all 3, not the 1 on screen',
    counts.length > 0 && counts[counts.length - 1] === 3, JSON.stringify(counts));
  check('reload: … and it is still a bare number, nothing else',
    POSTED3.filter(m => m.type === 'op_chat_count')
      .every(m => typeof m.n === 'number' && Object.keys(m).length === 2),
    JSON.stringify(POSTED3.filter(m => m.type === 'op_chat_count')));
}

(async () => {
  await storeCases();
  await domCases();
  await embedCases();
  await reloadCases();
  console.log(FAILURES.length ? ('\n' + FAILURES.length + ' FAILED') : '\nALL PASS');
  process.exitCode = FAILURES.length ? 1 : 0;
})();
