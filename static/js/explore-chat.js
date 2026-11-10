/* Explore — "Chat" (phase 3): a Telegram-like thread per report.
 *
 * Every answer /explore produces becomes ONE record in a per-report thread,
 * kept in this browser's IndexedDB (`op-explore` / `history`). The rows never
 * leave the browser: this file adds no new endpoint and no new POST — the
 * only server call it can make is the "Pin" one that already exists
 * (`POST /api/explore/questions`, which stores a title and a SQL string).
 *
 * Loaded AFTER static/js/explore.js and talks to it only through the
 * `window.OP_EXPLORE` page API — `ask`, `runQuery`, `renderResult`, `state`
 * and `on('answer', …)` are the documented five; the rest are the
 * deterministic helpers a thread needs so no period arithmetic, KPI lookup or
 * intent execution is re-implemented here.
 *
 * The top block is PURE — no DOM, no window, no IndexedDB — so
 * tests/explore_chat_dom.js can require this file in node and drive the record
 * shaping, the eviction, the key format and the follow-up matcher directly.
 * Same idiom as the top block of static/js/presentations.js.
 */
(function () {
  'use strict';

  // ═════════════════════════════════════════════════════════════════
  // Pure block — record shaping, the store's rules, the follow-up matcher
  // ═════════════════════════════════════════════════════════════════

  const DB_NAME = 'op-explore';
  const STORE = 'history';
  const DB_VERSION = 1;

  // A bubble shows at most three rows and a "… N more"; keeping the whole
  // result would turn a thread into a second copy of the folder. 200 is a
  // deliberate compromise: enough that "Expand" rehydrates a real grid
  // without re-running anything, small enough that 500 of them fit.
  const MAX_ROWS = 200;
  // Per thread, not per browser: one report's history is what an operator
  // scrolls; the oldest record of THAT thread is the one that goes.
  const MAX_RECORDS = 500;

  function chatKey(user, report, n) {
    return String(user) + ':' + String(report) + ':' + String(n);
  }

  // `#q=7` -> 7. Anything else -> null: an unparsable hash must leave screen A
  // exactly as it was rather than rehydrate record NaN.
  function parseQHash(hash) {
    const m = /(?:^|#)q=(\d+)(?:$|&)/.exec(String(hash == null ? '' : hash));
    if (!m) return null;
    const n = parseInt(m[1], 10);
    return isFinite(n) && n > 0 ? n : null;
  }

  function trimRows(rows) {
    const out = [];
    (rows || []).forEach(r => { if (out.length < MAX_ROWS) out.push(r); });
    return out;
  }

  // One thread record. Everything is plain JSON — a record must survive a
  // browser restart, and a live object graph (a KPI object holding the whole
  // semantics snapshot) neither structured-clones cheaply nor stays true.
  function shapeRecord(input) {
    const res = input.res || {};
    const rows = trimRows(res.rows);
    const prov = input.provenance || {};
    return {
      user: String(input.user),
      report_id: String(input.report_id),
      n: Number(input.n),
      ts: Number(input.ts || Date.now()),
      question: String(input.question == null ? '' : input.question),
      kind: String(input.kind || 'sql'),
      source: String(input.source || 'ask'),
      intent: input.intent ? JSON.parse(JSON.stringify(input.intent)) : null,
      kpi: input.kpi ? { key: input.kpi.key || null, name: input.kpi.name || null,
                         short: input.kpi.short || null,
                         definition: input.kpi.definition || null,
                         measure: input.kpi.measure || null,
                         unit: input.kpi.unit || null,
                         // P3a: the provenance footer names the owner of the
                         // definition the number was computed from.
                         owner: input.kpi.owner || null,
                         approved_at: input.kpi.approved_at || null } : null,
      sql: String(input.sql == null ? '' : input.sql),
      notes: (input.notes || []).slice(0, 4).map(x => String(x).slice(0, 300)),
      cols: (res.columns || []).slice(),
      rows: rows,
      // Honest about the trim: a bubble that says "3 of 12 000" must know it
      // is not holding 12 000 rows.
      total: res.total != null ? res.total : (res.rows || []).length,
      truncated: input.truncated === true || (res.rows || []).length > rows.length,
      provenance: {
        partitions_used: (prov.partitions_used || []).slice(),
        as_of: prov.as_of != null ? prov.as_of : null,
        files: (prov.files || []).slice(),
        // Phase 9.4: 'folder' | 'server' (+ the run behind a server index) —
        // frozen with the rest, so a reopened bubble still says whose data
        // it stood on.
        source: prov.source || null,
        run_id: prov.run_id != null ? prov.run_id : null,
      },
      // P3a — what the router knew: which form matched and how surely
      // (`match`), and which metrics it chose from (`shortlist`, the
      // "Clarify" alternatives). Both are small scalars; no rows ride here.
      match: input.match ? { score: input.match.score != null ? Number(input.match.score) : null,
                             runner_up: input.match.runner_up != null ? Number(input.match.runner_up) : null,
                             form: input.match.form != null ? String(input.match.form) : null } : null,
      shortlist: (input.shortlist || []).slice(0, 8).map(x => String(x)),
      // P3b, minor 11: the metric the READER pinned through "Clarify".
      // Without it "Regenerate" re-asked the same words with nothing
      // pinned — and the router was free to walk back to the metric the
      // reader had just rejected.
      fixed_kpi: input.fixed_kpi != null ? String(input.fixed_kpi) : null,
      // 👍/👎. `null` = not voted. The vote lives in the record either way;
      // `answer_id` is the server's own id of this answer (P4) and the only
      // thing that lets the vote leave this browser — an answer the server
      // recorded nothing for keeps a purely local vote.
      vote: input.vote === 'up' || input.vote === 'down' ? input.vote : null,
      answer_id: input.answer_id != null && isFinite(Number(input.answer_id))
        ? Number(input.answer_id) : null,
      // "Regenerate": this record replaced one, and that one is
      // `superseded` — it stays in history as a single quiet line.
      regen: input.regen === true,
      superseded: input.superseded === true,
      why: input.why ? JSON.parse(JSON.stringify(input.why)) : null,
      trend: input.trend ? JSON.parse(JSON.stringify(input.trend)) : null,
      parent_n: input.parent_n != null ? Number(input.parent_n) : null,
    };
  }

  // Which records fall off the end when one more is added. Returns the `n`s to
  // delete, oldest first — the caller turns them into keys, so the eviction
  // rule and the key format stay one decision each.
  function evictions(existing, cap) {
    const limit = cap || MAX_RECORDS;
    const sorted = (existing || []).slice().sort((a, b) => a.n - b.n);
    const over = sorted.length + 1 - limit;
    return over > 0 ? sorted.slice(0, over).map(r => r.n) : [];
  }

  // The store, over an INJECTABLE backend: node passes a fake, the page passes
  // the IndexedDB one below. Every rule that decides what a thread IS — the
  // key, the next `n`, the cap — lives here, where a test can reach it.
  //
  // backend: { list(user, report|null) -> Promise<record[]>,
  //            put(key, record) -> Promise, del(key) -> Promise }
  class ChatStore {
    constructor(opts) {
      this.backend = (opts || {}).backend;
      this.user = String((opts || {}).user == null ? 'anon' : opts.user);
      this.cap = (opts || {}).cap || MAX_RECORDS;
    }
    key(report, n) { return chatKey(this.user, report, n); }
    async list(report) {
      const rows = await this.backend.list(this.user, report == null ? null : String(report));
      return (rows || []).slice().sort((a, b) => a.n - b.n);
    }
    async get(report, n) {
      const rows = await this.list(report);
      return rows.find(r => Number(r.n) === Number(n)) || null;
    }
    async count(report) { return (await this.list(report)).length; }
    // Every conversation of one report. "New conversation" must not read as
    // "0 questions" in the viewer's badge: the reader's questions about this
    // report did not go anywhere, they are one screen away.
    async countBase(base) {
      const b = String(base);
      return (await this.list(null)).filter(r => baseOf(r.report_id) === b).length;
    }
    async clear(report) {
      const recs = await this.list(report);
      for (const r of recs) await this.backend.del(this.key(report, r.n));
      return recs.length;
    }
    // Threads = one row per report, newest activity first. `last` is what the
    // thread list shows beside the report title; `first` is what "Conversation
    // history" shows — a conversation is remembered by how it STARTED.
    async threads() {
      const rows = await this.list(null);
      const by = new Map();
      rows.forEach(r => {
        const cur = by.get(r.report_id);
        if (!cur) { by.set(r.report_id, { report_id: r.report_id, last: r, first: r, count: 0 }); return; }
        if (r.ts >= cur.last.ts) cur.last = r;
        if (r.ts < cur.first.ts || (r.ts === cur.first.ts && r.n < cur.first.n)) cur.first = r;
      });
      rows.forEach(r => { const tv = by.get(r.report_id); if (tv) tv.count += 1; });
      return Array.from(by.values()).sort((a, b) => b.last.ts - a.last.ts);
    }
    // Change a few fields of a record that already exists — a vote, or the
    // «superseded» mark a regenerate leaves on the answer it replaced. Never
    // a rewrite: the row is re-shaped from itself plus the patch, so a
    // malformed field cannot get in through the side door.
    async patch(report, n, fields) {
      const rid = String(report);
      const rec = await this.get(rid, n);
      if (!rec) return null;
      const next = shapeRecord(Object.assign({}, rec, fields || {}, {
        user: rec.user, report_id: rec.report_id, n: rec.n, ts: rec.ts,
        res: { columns: rec.cols, rows: rec.rows, total: rec.total },
        provenance: rec.provenance,
      }));
      await this.backend.put(this.key(rid, n), next);
      return next;
    }
    // Writes are SERIALISED. Two answers that land in the same tick (a
    // "Regenerate" while the first version is still being written) both
    // read the same «last n» and the second silently overwrote the first
    // under one key. The queue is per store, so a thread's numbering is a
    // sequence and not a race.
    add(report, payload) {
      const next = () => this.addNow(report, payload);
      const run = this.chain ? this.chain.then(next, next) : next();
      this.chain = run.then(() => undefined, () => undefined);
      return run;
    }
    async addNow(report, payload) {
      const rid = String(report);
      const existing = await this.list(rid);
      const n = existing.length ? existing[existing.length - 1].n + 1 : 1;
      const rec = shapeRecord(Object.assign({}, payload, {
        user: this.user, report_id: rid, n: n,
      }));
      await this.backend.put(this.key(rid, n), rec);
      for (const gone of evictions(existing, this.cap)) {
        await this.backend.del(this.key(rid, gone));
      }
      return rec;
    }
  }

  // ── the follow-up matcher (client first) ──────────────────────────────
  // "and by week?" after "what was SL in July?" needs no model and no round
  // trip: the metric is already settled by the OPEN answer, and the question
  // only changes the cut or the period. Three patterns, all deterministic:
  //
  //   granularity  "by week / by month / by day / by year" (Russian phrasing)
  //   dim          "and by <dimension>" against the KPI's own dim names
  //   period       a month by Russian name, a year as 20\d\d
  //
  // The guard that makes this safe: a question carrying METRIC words is never
  // a follow-up — "what was SL by week?" names a metric and must go through
  // the normal routing, where the server may well pick a different KPI.

  const RU_MONTHS = [
    ['январ', 1], ['феврал', 2], ['март', 3], ['апрел', 4], ['ма', 5], ['июн', 6],
    ['июл', 7], ['август', 8], ['сентябр', 9], ['октябр', 10], ['ноябр', 11], ['декабр', 12],
  ];

  // "by week" -> week. The stems are the ADR-13 time-dim convention
  // (year/month/week/day) spelled in Russian, nothing report-specific.
  //
  // Matched over TOKENS, not with `\b`: JavaScript's word boundary is defined
  // over `[A-Za-z0-9_]`, so `\b` before a Cyrillic stem never matches after a space in a Russian
  // sentence at all. (Python's `re` is Unicode-aware and the server's
  // `_question_times` can use `\b` safely — this is the one place the two
  // halves cannot be spelled the same way.)
  const GRAIN = [
    { dim: 'week', stems: ['недел'] },
    { dim: 'month', stems: ['месяц'] },
    { dim: 'day', stems: ['дн', 'ден', 'сутк', 'сутки', 'дат'] },
    { dim: 'year', stems: ['год', 'лет'] },
  ];

  // Words that can never be a metric: function words, the question's own
  // scaffolding, and the granularity/period nouns the matcher reads itself.
  const STOP = new Set([
    'а', 'и', 'в', 'во', 'на', 'по', 'за', 'с', 'со', 'к', 'у', 'о', 'об', 'из', 'от', 'до',
    'же', 'ли', 'то', 'что', 'как', 'так', 'вот', 'ещё', 'еще', 'там', 'тут', 'это', 'этот',
    'мне', 'нам', 'покажи', 'выведи', 'дай', 'сделай', 'хочу', 'нужно', 'надо',
    'разрез', 'разрезе', 'срез', 'срезе', 'детал', 'детали',
    'недел', 'неделя', 'неделям', 'неделе', 'неделю', 'недели',
    'месяц', 'месяцам', 'месяце', 'месяца', 'месяцы',
    'день', 'дням', 'дни', 'дня', 'дне', 'сутки', 'суткам', 'дата', 'датам',
    'год', 'году', 'годам', 'года', 'годы', 'лет',
  ]);

  function normWords(text) {
    return String(text == null ? '' : text).toLowerCase().replace(/ё/g, 'е')
      .split(/[^a-zа-я0-9]+/).filter(Boolean);
  }

  // Inflection-tolerant without a stemmer: two Russian words count as the same
  // word when they agree on their first four letters (two case forms of "orders").
  // Short tokens ("SL", the "pcs" abbreviation) must match exactly — a 4-char prefix of a 2-char
  // word is the word itself, and truncating would make "SL" match a longer word such as "slot".
  function stemKey(w) { return w.length >= 4 ? w.slice(0, 4) : w; }

  function isPeriodWord(w) {
    if (/^20\d\d$/.test(w)) return true;
    return RU_MONTHS.some(([stem]) => (stem === 'ма' ? /^ма[яе]$/.test(w) : w.indexOf(stem) === 0));
  }

  // The word right after each Russian "by" preposition — the shape both follow-up cuts take
  // ("by week", "by warehouse").
  function afterPo(tokens) {
    const out = [];
    tokens.forEach((w, i) => { if (w === 'по' && tokens[i + 1]) out.push(tokens[i + 1]); });
    return out;
  }

  // Every phrase the report's own KPIs may be recognised by, as stem keys.
  // Same source as the server's `match_forms`: the bundle's semantics, never
  // platform code.
  function metricKeys(forms) {
    const out = new Set();
    (forms || []).forEach(f => normWords(f).forEach(w => {
      if (w.length < 2 || STOP.has(w) || isPeriodWord(w)) return;
      out.add(stemKey(w));
    }));
    return out;
  }

  function hasMetricWords(question, forms) {
    const keys = metricKeys(forms);
    if (!keys.size) return false;
    return normWords(question).some(w => {
      if (w.length < 2 || STOP.has(w) || isPeriodWord(w)) return false;
      return keys.has(stemKey(w));
    });
  }

  // The period the question itself names — the JS half of
  // `nl2sql._question_times`, minus the week arithmetic the client has no
  // calendar for. Only what is READ can ever become a filter.
  // Ambiguity is NOT resolved here: "June or July" names two months and no
  // single one of them is what the operator asked for, so neither is taken and
  // the question falls through to the normal (model) routing.
  function questionTimes(question) {
    const toks = normWords(question);
    const out = {};
    const years = Array.from(new Set(toks.filter(tv => /^20\d\d$/.test(tv)).map(Number)));
    if (years.length === 1) out.year = years[0];
    const months = new Set();
    toks.forEach(tv => {
      RU_MONTHS.forEach(([stem, num]) => {
        if (stem === 'ма') { if (/^ма[яе]$/.test(tv)) months.add(5); return; }
        if (tv.indexOf(stem) === 0) months.add(num);
      });
    });
    if (months.size === 1) out.month = months.values().next().value;
    return out;
  }

  // `{dims, filters, matched}` when the question is a deterministic follow-up
  // over `prevIntent`, else null.
  //
  // ctx: { dims: [...the KPI's own dims], synonyms: {dim: [word, …]} }
  //      forms: [...every question form / name of every ready KPI]
  // Interrogatives: a follow-up refines ("and by week?", "for 2025"); a
  // sentence that asks is a question of its own.
  const NEW_QUESTION_RE = /(?:^|[^а-яa-z])(?:как(?:ой|ая|ое|ие|ом|ую|их|им)|сколько|кто|где|когда|почему|котор[а-я]+|чем|что)(?![а-я])/;
  // "for the whole of 2026", "in 2026", "for the year" — the year as a SPAN: the
  // finer time filters (month/week/day) of the open answer go away, not
  // just the year. A bare "for 2025" keeps them (July 2025 after July 2026).
  const YEAR_SPAN_RE = /(?:^|[^а-я])(?:весь|целый|год[ау]?)(?![а-я])|с\s+начала\s+года/;

  function matchFollowUp(question, prevIntent, ctx) {
    const q = String(question == null ? '' : question).toLowerCase().replace(/ё/g, 'е');
    if (!q.trim() || !prevIntent || !prevIntent.kpi) return null;
    const c = ctx || {};
    // The guard: a question that names a metric is a NEW question.
    if (hasMetricWords(q, c.forms)) return null;
    // A year the report's OWN forms name ("SL for 2025", "2025 Total") is
    // a metric of its own, not a period of the open answer: "and in 2025?"
    // after a current-year SL must reach the form matcher, which knows the
    // 2025 KPI — filtering this year's rows by 2025 sums nothing and used to
    // render an empty card (stand 2026-09-06).
    const years = q.match(/(?:^|[^\d])(20\d\d)(?!\d)/g) || [];
    if (years.length && (c.forms || []).some(f => years.some(y => String(f).indexOf(y.replace(/\D/g, '')) >= 0))) return null;
    // So is one that ASKS something: "in which week was the lowest … for
    // Delta?" is not a cut of the open answer whatever words it shares
    // with one (prod 2026-08-28: it re-ran the previous metric unchanged).
    if (NEW_QUESTION_RE.test(q)) return null;
    const allowed = (c.dims || []).map(String);
    const matched = [];
    let dims = (prevIntent.dims || []).slice();
    const wanted = afterPo(normWords(q));

    // 1. granularity
    for (const g of GRAIN) {
      if (!wanted.some(w => g.stems.some(s => w.indexOf(s) === 0))) continue;
      if (allowed.indexOf(g.dim) < 0) return null;   // this KPI has no such cut
      dims = [g.dim];
      matched.push('granularity');
      break;
    }

    // 2. "and by <dimension>" — the dim names the report itself declares, plus
    //    whatever synonyms its semantics carry. No platform vocabulary.
    if (!matched.length) {
      for (const w of wanted) {
        const key = stemKey(w);
        const hit = allowed.find(d => {
          if (stemKey(String(d).toLowerCase()) === key) return true;
          const syn = (c.synonyms || {})[d] || [];
          return syn.some(s => stemKey(String(s).toLowerCase().replace(/ё/g, 'е')) === key);
        });
        if (hit) { dims = [hit]; matched.push('dim'); break; }
      }
    }

    // 3. period — replaces the time filters, keeps every other one.
    let filters = (prevIntent.filters || []).map(f => Object.assign({}, f));
    const times = questionTimes(q);
    const timeDims = Object.keys(times);
    if (timeDims.length) {
      const usable = timeDims.filter(d => allowed.indexOf(d) >= 0);
      if (usable.length) {
        let drop = usable.slice();
        if (usable.indexOf('year') >= 0 && !times.month && YEAR_SPAN_RE.test(q)) {
          drop = drop.concat(['month', 'week', 'day']);
        }
        filters = filters.filter(f => drop.indexOf(String(f.dim)) < 0);
        usable.forEach(d => filters.push({ dim: d, op: '=', value: times[d] }));
        matched.push('period');
      }
    }

    if (!matched.length) return null;
    return { dims: dims, filters: filters, matched: matched };
  }


  // ═════════════════════════════════════════════════════════════════
  // P3a — what a card SAYS about itself. All of it pure: the card, the
  // "no empty cards" invariant and the node tests read the same
  // functions, so a badge cannot mean one thing on screen and another in a
  // test. Nothing here touches the DOM or the network.
  // ═════════════════════════════════════════════════════════════════

  // Which column holds the number: the engine's own `value` (intent_sql
  // always emits it), else the last column.
  const NON_LABEL = { value: 1, n: 1, d: 1, value_pieces: 1 };
  function valueIndex(rec) {
    const cols = (rec && rec.cols) || [];
    const i = cols.indexOf('value');
    return i >= 0 ? i : cols.length - 1;
  }
  function labelIndex(rec, vi) {
    const cols = (rec && rec.cols) || [];
    for (let i = 0; i < cols.length; i++) {
      if (i !== vi && cols[i] !== 'n' && cols[i] !== 'd') return i;
    }
    return -1;
  }
  // Every cut column of a row, joined: «R&W · H15», not three rows of «R&W».
  function rowLabel(rec, row, vi) {
    const parts = [];
    ((rec && rec.cols) || []).forEach((c, i) => {
      if (i === vi || NON_LABEL[c]) return;
      const v = row[i];
      if (v == null || v === '') return;
      parts.push(String(v));
    });
    return parts.join(' · ');
  }

  const TIME_DIM_RE = /^(year|month|week|day|quarter)$/;

  // nl2sql.FORM_MIN_SCORE — the server will not answer from a question form
  // below this, so anything at or above it is an "exact form" and the reader
  // is told no model was involved.
  const FORM_SURE = 4;

  // The badge: WHO chose the metric and how surely. Three tiers, in the
  // order of how much the reader has to trust: the report's own question
  // form, a model reading the question, free SQL over the semantics.
  function tierBadge(rec) {
    const src = String((rec && rec.source) || '');
    const kind = String((rec && rec.kind) || '');
    const m = (rec && rec.match) || null;
    if (kind === 'error') return { tier: 'error', cls: 'is-bad', text: t('no answer') };
    if (kind === 'sql' || /semantic|nl2sql/.test(src)) {
      return { tier: 'semantic_sql', cls: 'is-draft', text: t('SQL from semantics') };
    }
    if (src === 'form' || (m && m.form)) {
      const sure = !m || m.score == null || Number(m.score) >= FORM_SURE;
      return sure ? { tier: 'form', cls: '', text: t('exact form') }
                  : { tier: 'form', cls: 'is-model', text: t('the form matched loosely') };
    }
    if (/model|llm/.test(src)) return { tier: 'model', cls: 'is-model', text: t('understood by meaning') };
    return { tier: 'form', cls: '', text: t('by the report\'s definition') };
  }

  // The second half of the footer line: how sure, in numbers when there are
  // numbers. `null` when there is nothing honest to say.
  function confidenceText(rec) {
    const m = (rec && rec.match) || null;
    if (m && m.score != null) {
      return m.runner_up != null
        ? t('match {p1}, next {p2}', {p1: m.score, p2: m.runner_up})
        : t('match {p1}', {p1: m.score});
    }
    if (/model|llm/.test(String((rec && rec.source) || ''))) return t('model selection');
    return null;
  }

  // «The reading was not strict» — WHY, in one word, or `false`.
  //
  //   'match'  two candidate metrics scored within one point of each other
  //   'model'  a MODEL picked this metric out of the menu `rank_kpis` built,
  //            and the menu held someone else too
  //
  // Adversarial P3b, major 2: the numeric rule alone never fired. `match` has
  // exactly one producer — `nl2sql.match_forms` — and it returns a hit only
  // when `best >= FORM_MIN_SCORE` AND `best >= FORM_LEAD * runner_up`, so
  // «score below the threshold» and «within one point of the runner-up» are
  // both unreachable for any record the server can send. The reachable
  // signal is who chose: a form match IS the confident case, and a model
  // choosing between several shortlisted metrics is a guess the card must
  // own up to. The numeric branches stay as a guard for a future producer
  // that does report a rejected hit.
  function lowConfidence(rec) {
    const m = (rec && rec.match) || null;
    if (m && m.score != null) {
      if (m.runner_up != null && Number(m.score) - Number(m.runner_up) < 1) return 'match';
      if (String((rec && rec.source) || '') === 'form' && Number(m.score) < FORM_SURE) return 'match';
    }
    if (!/model|llm/.test(String((rec && rec.source) || ''))) return false;
    // A menu of one is no choice: the model had nothing else to pick, so
    // there is nothing to offer and nothing to warn about.
    const used = String((((rec && rec.intent) || {}).kpi) == null ? '' : rec.intent.kpi);
    const rivals = ((rec && rec.shortlist) || []).filter(k => String(k) !== used);
    return rivals.length ? 'model' : false;
  }

  // "Understood as": metric, cut, period, filters — NAMES only, never values of
  // the answer itself. This is the line an operator checks before believing
  // the number.
  function understood(rec) {
    const it = (rec && rec.intent) || {};
    const k = (rec && rec.kpi) || {};
    const out = [];
    const name = k.short || k.name || it.kpi;
    if (name) out.push([t('metric'), String(name)]);
    const dims = (it.dims || []).map(String);
    const cuts = dims.filter(d => !TIME_DIM_RE.test(d));
    const grains = dims.filter(d => TIME_DIM_RE.test(d));
    if (cuts.length) out.push([t('dimension'), cuts.join(', ')]);
    else if (!dims.length && (rec && rec.kind !== 'sql')) out.push([t('dimension'), t('the metric\'s total')]);
    const filters = (it.filters || []).filter(Boolean);
    const per = filters.filter(f => TIME_DIM_RE.test(String(f.dim)))
      .map(f => String(f.dim) + ' ' + (f.value != null ? f.value : (f.values || []).join(',')));
    grains.forEach(g => per.push(t('by {p1}', {p1: g})));
    if (per.length) out.push([t('period'), per.join(' · ')]);
    filters.filter(f => !TIME_DIM_RE.test(String(f.dim))).slice(0, 3).forEach(f => {
      out.push([String(f.dim), String(f.value != null ? f.value : (f.values || []).join(', '))]);
    });
    return out;
  }

  // THE INVARIANT (tested): an answer that carries no number is never a
  // card with a blank body. 'empty' — the query returned nothing; 'null' —
  // an aggregate over zero rows (SUM of nothing is NULL). Either way the
  // card becomes a question back to the operator.
  function clarifyReason(rec) {
    if (!rec) return null;
    const kind = String(rec.kind || '');
    if (kind === 'error' || kind === 'why' || kind === 'trend') return null;
    const rows = rec.rows || [];
    if (!rows.length) return 'empty';
    if (rows.length === 1) {
      const v = rows[0][valueIndex(rec)];
      if (v == null || v === '') return 'null';
    }
    return null;
  }

  // What to offer instead of the emptiness. Every option is something this
  // browser can actually do — a filter it can drop, a metric the router
  // itself shortlisted, or the composer.
  //   {act:'allperiod'}          drop every time filter
  //   {act:'nofilter', arg:dim}  drop one non-time filter
  //   {act:'alt', arg:kpiKey}    another metric from the shortlist
  //   {act:'ask'}                back to the composer
  function clarifyOptions(rec) {
    const it = (rec && rec.intent) || {};
    const filters = (it.filters || []).filter(Boolean);
    const out = [];
    if (filters.some(f => TIME_DIM_RE.test(String(f.dim)))) {
      out.push({ act: 'allperiod', hint: t('remove the period filter') });
    }
    filters.filter(f => !TIME_DIM_RE.test(String(f.dim))).slice(0, 2).forEach(f => {
      out.push({ act: 'nofilter', arg: String(f.dim), hint: t('remove this filter') });
    });
    (rec && rec.shortlist ? rec.shortlist : []).forEach(key => {
      if (out.filter(o => o.act === 'alt').length >= 3) return;
      if (String(key) === String(it.kpi)) return;
      out.push({ act: 'alt', arg: String(key), hint: t('a different metric') });
    });
    out.push({ act: 'ask', hint: t('back to the input') });
    return out;
  }

  // Follow-up suggestions, deterministic: the cuts this metric HAS and this
  // answer did not use. No model, no round trip — clicking one goes through
  // the same follow-up matcher a typed "and by warehouse?" does.
  function followUps(rec, kpiDims) {
    const used = ((rec && rec.intent && rec.intent.dims) || []).map(String);
    const out = [];
    (kpiDims || []).map(String).forEach(d => {
      if (out.length >= 3) return;
      if (used.indexOf(d) >= 0) return;
      if (TIME_DIM_RE.test(d) && d !== 'week' && d !== 'month') return;
      out.push(t('by {p1}', {p1: d}));
    });
    return out;
  }

  // The coverage pill, from the index THIS browser already holds: partition
  // keys and the row count the last index reported. `fmtInt` formats the
  // number in the reader's locale; without one it is a bare String().
  function coverageOf(stats, tables, fmtInt) {
    const fmt = typeof fmtInt === 'function' ? fmtInt : (n => String(n));
    const keys = [];
    (tables || []).forEach(tv => (tv.partitions || []).forEach(pt => {
      const k = pt && pt.key != null ? String(pt.key) : '';
      if (k && keys.indexOf(k) < 0) keys.push(k);
    }));
    let rows = Number((stats || {}).rows || 0);
    if (!rows) rows = (tables || []).reduce((a, tv) => a + (Number(tv.rows) || 0), 0);
    const parts = [];
    if (keys.length && keys.length <= 3) parts.push(keys.join(', '));
    else if (keys.length) parts.push(tn(keys.length, '{n} partition|{n} partitions'));
    if (rows > 0) parts.push(tn(rows, '{n} row|{n} rows', { n: fmt(rows) }));
    return parts.join(' · ');
  }

  // ═════════════════════════════════════════════════════════════════
  // P3b — "Clarify", the idempotency keys, conversations, the transcript.
  // Pure, for the same reason the block above is: a dedup rule that lives
  // in an event handler cannot be tested, and one that is wrong doubles
  // every record in this browser's history.
  // ═════════════════════════════════════════════════════════════════

  // djb2/xor over a string — no library, no crypto, and stable across
  // reloads. It only has to separate two questions from each other.
  function hashKey(s) {
    const str = String(s == null ? '' : s);
    let h = 5381;
    for (let i = 0; i < str.length; i++) h = (((h << 5) + h) ^ str.charCodeAt(i)) >>> 0;
    return h.toString(36);
  }

  // Everything that can make the SAME words a DIFFERENT question: the card it
  // hangs off, the metric it is pinned to, and whether it is a deliberate
  // re-run. Two questions with one key are ONE request: the second caller
  // waits for the first answer instead of starting a second engine run.
  // `report` — the thread the question is being asked IN. The same words in
  // a new conversation are a new question: without it, "New conversation" then
  // re-asking would have joined the answer still in flight in the old one.
  function askKey(question, opts) {
    const o = opts || {};
    const norm = String(question == null ? '' : question).trim().toLowerCase().replace(/\s+/g, ' ');
    return hashKey(norm + '|' + JSON.stringify({
      report: o.report != null ? String(o.report) : null,
      parent: o.parentN != null ? Number(o.parentN) : null,
      fixed: o.fixedKpi != null ? String(o.fixedKpi) : null,
      regen: o.regen === true,
    }));
  }

  // Risk 5 of the plan: the follow-up branch and the main path can both emit
  // an 'answer' for ONE question — the thread would keep two identical
  // records and the pane's counter would say two. A deliberate
  // "Regenerate" is NOT a duplicate: it carries `regen`, so it has a
  // key of its own and always lands.
  function answerKey(ev) {
    const e = ev || {};
    return String(e.question == null ? '' : e.question) + '\u0000'
      + String(e.parent_n == null ? '' : e.parent_n) + '\u0000'
      + (e.regen === true ? '1' : '0');
  }

  // ── conversations inside one report ───────────────────────────────────
  // "New conversation" deletes nothing: the old thread stays and the new one is
  // a SECOND thread of the same report. The conversation rides in the report
  // id after a «#», so everything that needs the report itself (its title,
  // its semantics, its KPIs) reads `baseOf` and nothing else changes.
  function convKey(base, conv) {
    return conv ? String(base) + '#' + String(conv) : String(base);
  }
  function baseOf(rid) {
    const s = String(rid == null ? '' : rid);
    const i = s.indexOf('#');
    return i < 0 ? s : s.slice(0, i);
  }
  function convOf(rid) {
    const s = String(rid == null ? '' : rid);
    const i = s.indexOf('#');
    return i < 0 ? null : s.slice(i + 1);
  }

  // ── "Clarify": the alternatives, and how well each one fits ──────────
  // The router's `shortlist` is a list of KEYS in its own order and carries
  // no scores, so the fit is computed here the way `nl2sql.rank_kpis`
  // computes it: word overlap between the question and the KPI's own forms,
  // name and definition. No request, nothing report-specific.
  function fitScore(question, kpi) {
    const q = new Set(normWords(question)
      .filter(w => w.length > 2 && !STOP.has(w) && !isPeriodWord(w)).map(stemKey));
    if (!q.size || !kpi) return 0;
    // Both halves of `rank_kpis` are here, or the shortlist and the scoring
    // disagree: the server raised a candidate BY ITS DIMS ("by warehouse" ->
    // a KPI cut by a "warehouses" dim) and `shortlistAlts` then dropped it as noise
    // for scoring below 1. `dims` are part of the text, and a long question
    // word standing inside a question form is worth the same +2 it is worth
    // on the server.
    const text = [kpi.name, kpi.short, kpi.definition]
      .concat(kpi.question_forms || []).concat(kpi.dims || []).filter(Boolean).join(' ');
    const words = new Set(normWords(text).filter(w => w.length > 2).map(stemKey));
    const forms = normWords((kpi.question_forms || []).join(' ')).join(' ');
    let n = 0;
    q.forEach(w => {
      if (words.has(w)) n += 1;
      if (w.length > 3 && forms.indexOf(w) >= 0) n += 2;
    });
    return n;
  }

  // `[{key, score, pct}]`, best first. The metric the answer already used is
  // never offered as an alternative to itself, and a candidate the question
  // shares no word with (score below 1) is not an alternative — it is noise
  // the router happened to keep in the prompt menu.
  // `kpiFor(key)` -> the KPI node out of the semantics; no lookup is done here.
  function shortlistAlts(rec, kpiFor) {
    const it = (rec && rec.intent) || {};
    const used = String(it.kpi == null ? '' : it.kpi);
    const question = (rec && rec.question) || '';
    const get = typeof kpiFor === 'function' ? kpiFor : (() => null);
    const out = [];
    ((rec && rec.shortlist) || []).forEach(key => {
      const k = String(key);
      if (k === used || out.some(o => o.key === k)) return;
      const score = fitScore(question, get(k) || {});
      if (score < 1) return;
      out.push({ key: k, score: score });
    });
    // The percentage is «as good as the best reading of this question», not a
    // probability: the answer's own metric is part of the scale, so an
    // alternative can never claim 100 % against a metric that fit better.
    const top = out.reduce((m, o) => (o.score > m ? o.score : m), 0);
    const own = fitScore(question, get(used) || {});
    const base = Math.max(top, own, 1);
    out.forEach(o => { o.pct = Math.max(1, Math.min(100, Math.round((o.score / base) * 100))); });
    out.sort((a, b) => b.score - a.score);
    return out.slice(0, 4);
  }

  // H10 — the emptiness has a NAME: the period the question filtered by is
  // not in the index this browser holds. `keys` are the partition keys of the
  // loaded index; if not one of them mentions the year (or the month) that
  // was filtered on, the answer is empty because the data is not here.
  // A partition key read as the periods it actually stands for:
  // `'2026-06'` -> {year: 2026, month: 6}, `'2026-06-03'` -> + {day: 3},
  // `'week 23'` (Russian label) / `'2026-W23'` -> {week: 23}, `'2026'` -> {year: 2026}.
  // `null` for a key shaped like nothing above — the card then says nothing
  // at all, which is the only honest answer about a key we cannot read.
  // The «w» must start a word, or `sw2024` would be week 20.
  const WEEK_KEY_RE = /(?:^|[^a-zа-яё])(?:недел[а-я]*|week|w)[\s._#-]*(\d{1,2})(?!\d)/i;
  const DAY_KEY_RE = /(?:^|\D)(\d{4})\D(\d{1,2})\D(\d{1,2})(?!\d)/;
  const MONTH_KEY_RE = /(?:^|\D)(\d{4})\D(\d{1,2})(?!\d)/;
  const YEAR_KEY_RE = /(?:^|\D)(\d{4})(?!\d)/;
  function partitionFacets(key) {
    const s = String(key == null ? '' : key);
    const out = {};
    const wk = s.match(WEEK_KEY_RE);
    if (wk) {
      const n = Number(wk[1]);
      if (n >= 1 && n <= 53) out.week = n;
    }
    const day = s.match(DAY_KEY_RE);
    const mon = day || s.match(MONTH_KEY_RE);
    if (mon && Number(mon[2]) >= 1 && Number(mon[2]) <= 12) {
      out.year = Number(mon[1]);
      out.month = Number(mon[2]);
      if (day && Number(day[3]) >= 1 && Number(day[3]) <= 31) out.day = Number(day[3]);
    } else {
      const yr = s.match(YEAR_KEY_RE);
      if (yr) out.year = Number(yr[1]);
    }
    return Object.keys(out).length ? out : null;
  }

  // H10 — the emptiness has a NAME: the period the question filtered by is
  // not in the index this browser holds.
  //
  // Adversarial P3b, major 3: this used to be `key.indexOf(String(value))`,
  // which read "week 27" out of `'2026-07'`, "year 2026" out of nothing at
  // all, and generally answered a question about WEEKS with a shelf full of
  // MONTHS. The filter and the key are now compared at the same granularity
  // and only there: keys that say nothing about the dim being filtered
  // (monthly partitions asked about a week) prove nothing, so the card stays
  // quiet rather than guessing.
  function periodOutsideCoverage(rec, keys) {
    const it = (rec && rec.intent) || {};
    const times = (it.filters || []).filter(f => f && TIME_DIM_RE.test(String(f.dim))
      && (f.op == null || f.op === '=') && f.value != null && String(f.value).length);
    const facets = (keys || []).map(partitionFacets).filter(Boolean);
    if (!times.length || !facets.length) return false;
    return times.some(f => {
      const dim = String(f.dim);
      const v = Number(f.value);
      if (!isFinite(v)) return false;
      const known = facets.filter(fc => fc[dim] != null);
      if (!known.length) return false;      // this index cannot speak of that dim
      return !known.some(fc => fc[dim] === v);
    });
  }

  // A cut (or a filter) the metric does not have. The engine answers nothing
  // or drops it silently, and "empty" is not the reason — the card names the
  // cut and offers the ones the metric really has.
  function unavailableDims(rec, kpiDims) {
    const it = (rec && rec.intent) || {};
    const have = (kpiDims || []).map(String);
    if (!have.length) return [];
    const used = (it.dims || []).map(String)
      .concat((it.filters || []).filter(Boolean).map(f => String(f.dim)));
    const out = [];
    used.forEach(d => { if (have.indexOf(d) < 0 && out.indexOf(d) < 0) out.push(d); });
    return out;
  }

  // ── "Copy conversation" ────────────────────────────────────────────
  // What a person pastes into a letter: the questions and the numbers. NEVER
  // the SQL — a query is not the answer, and a pasted one carries the shape
  // of the data to wherever the letter goes.
  // Adversarial P3b, major 4: "A:" used to carry up to five LABELLED rows —
  // the values of the dim columns, which is exactly the data this pane
  // promises never to hand anywhere. A one-row answer with no label is a
  // scalar and a scalar is the answer; anything with rows is named as a
  // table and counted, never quoted. The notes (the engine's own remarks)
  // stay: they are the page's words, not the data's.
  function answerLine(rec, fmt) {
    const rows = (rec && rec.rows) || [];
    if (!rec || rec.kind === 'error' || !rows.length) {
      return ((rec && rec.notes) || [])[0] || t('no answer {ctx:2}');
    }
    const vi = valueIndex(rec);
    if (rows.length === 1 && labelIndex(rec, vi) < 0) return String(fmt(rows[0][vi], rec.kpi));
    const n = Number(rec.total) > rows.length ? Number(rec.total) : rows.length;
    return tn(n, 'table, {n} row|table, {n} rows');
  }
  function transcriptOf(recs, fmt) {
    const f = typeof fmt === 'function' ? fmt : (v => String(v == null ? '' : v));
    const out = [];
    (recs || []).forEach(rec => {
      if (rec && rec.superseded) return;
      out.push(t('Q: {p1}', {p1: String((rec && rec.question) == null ? '' : rec.question)}));
      out.push(t('A: {p1}', {p1: answerLine(rec, f)}));
      out.push('');
    });
    return out.join('\n').trim();
  }

  // Node test hook only — exported before anything below touches
  // document/window/indexedDB, so the pure half requires cleanly.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      ChatStore, chatKey, parseQHash, shapeRecord, evictions, matchFollowUp,
      hasMetricWords, questionTimes, normWords, stemKey,
      valueIndex, labelIndex, rowLabel, tierBadge, confidenceText, lowConfidence,
      understood, clarifyReason, clarifyOptions, followUps, coverageOf,
      hashKey, askKey, answerKey, convKey, baseOf, convOf,
      fitScore, shortlistAlts, periodOutsideCoverage, partitionFacets, unavailableDims,
      answerLine, transcriptOf,
      MAX_ROWS, MAX_RECORDS, DB_NAME, STORE, FORM_SURE,
    };
  }

  if (typeof document === 'undefined') return;   // node require() path stops here

  const OP = window.OP_EXPLORE;
  const chatSection = document.getElementById('ex-chat');
  if (!OP || !chatSection) return;

  // ═════════════════════════════════════════════════════════════════
  // Embed mode (phase 4) — this page inside the viewer's "Chat" pane
  // ═════════════════════════════════════════════════════════════════
  // `/explore?report=<dashboard_id>&mode=chat&embed=1` is the SAME page in a
  // same-origin iframe beside an open report: same origin, so the same
  // IndexedDB, so literally the same thread as /explore. The three params
  // arrive as `#ex-root` data-attributes and change exactly three things:
  // which thread is open, that the chat view opens straight away, and that
  // the parent is told the record count (the one thing that crosses frames).
  const exRoot = document.getElementById('ex-root');
  const EMBED = !!(exRoot && exRoot.dataset.embed === '1');
  const EMBED_REPORT = (exRoot && exRoot.dataset.report) || '';
  const EMBED_MODE = (exRoot && exRoot.dataset.mode) || '';

  // A thread is keyed by GENERATOR (that is what a semantics/KPI belongs to)
  // while the viewer knows a DASHBOARD; the reports snapshot the server
  // already baked into the page carries both, so no lookup is added.
  function reportGenOf(dashId) {
    if (!dashId || !exRoot) return null;
    let reports = [];
    try { reports = JSON.parse(exRoot.dataset.reports || '[]') || []; } catch (e) { reports = []; }
    const hit = reports.find(r => String(r.dashboard_id) === String(dashId));
    return hit ? String(hit.generator_id) : null;
  }

  // The ONLY thing this page posts to its parent: how many records the open
  // thread holds. No rows, no question text, no SQL — the parent renders it
  // as a number and nothing else. Same-origin target, never '*'.
  function postCount(n) {
    if (!EMBED) return;
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: 'op_chat_count', n: Number(n) || 0 }, location.origin);
      }
    } catch (e) { /* a parent that went away is not an error here */ }
  }

  // Two more messages up, both of them geometry, both of them scalars: the
  // parent owns the pane's width, so Escape INSIDE the frame (where the
  // parent's keydown never fires) has to ask the parent to close. It only
  // asks when the composer is empty — the first Escape clears a draft, the
  // second closes the pane.
  function postToPane(msg) {
    if (!EMBED) return;
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(msg, location.origin);
      }
    } catch (e) { /* a parent that went away is not an error here */ }
  }
  if (EMBED) {
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      // An open «⋯» is what Escape closes first — the menu's own handler does
      // it, this one only steps out of the way.
      const menu = document.getElementById('ex-chat-menu');
      if (menu && !menu.hidden) return;
      const input = document.getElementById('ex-chat-input');
      if (input && document.activeElement === input && String(input.value || '').trim()) {
        input.value = '';           // first Escape: drop the draft
        return;
      }
      postToPane({ type: 'op_chat_close' });
    });
  }

  // ═════════════════════════════════════════════════════════════════
  // IndexedDB backend — the ONE place this file talks to storage
  // ═════════════════════════════════════════════════════════════════
  function idbOpen() {
    return new Promise((res, rej) => {
      let req;
      try { req = indexedDB.open(DB_NAME, DB_VERSION); } catch (e) { rej(e); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        const st = db.objectStoreNames.contains(STORE)
          ? req.transaction.objectStore(STORE)
          : db.createObjectStore(STORE);
        if (!st.indexNames.contains('by_thread')) {
          st.createIndex('by_thread', ['user', 'report_id']);
        }
      };
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
  }

  // An array key sorts after every scalar in IndexedDB's key ordering, so
  // `[user] … [user, []]` is exactly "every record of this user, whatever the
  // report" — the same index serves one thread and the thread list.
  const idbBackend = {
    async list(user, report) {
      const db = await idbOpen();
      return new Promise((res, rej) => {
        const idx = db.transaction(STORE, 'readonly').objectStore(STORE).index('by_thread');
        const range = report == null
          ? IDBKeyRange.bound([user], [user, []])
          : IDBKeyRange.only([user, String(report)]);
        const r = idx.getAll(range);
        r.onsuccess = () => res(r.result || []);
        r.onerror = () => rej(r.error);
      });
    },
    async put(key, rec) {
      const db = await idbOpen();
      return new Promise((res, rej) => {
        const tv = db.transaction(STORE, 'readwrite');
        tv.objectStore(STORE).put(rec, key);
        tv.oncomplete = () => res();
        tv.onerror = () => rej(tv.error);
      });
    },
    async del(key) {
      const db = await idbOpen();
      return new Promise((res, rej) => {
        const tv = db.transaction(STORE, 'readwrite');
        tv.objectStore(STORE).delete(key);
        tv.oncomplete = () => res();
        tv.onerror = () => rej(tv.error);
      });
    },
  };

  // ══════════════════════════════════════════════════════════════════
  // TEST-ONLY hook, same discipline as explore.js's `__opExploreTestFiles`:
  // there is no IndexedDB in the node DOM harness, so it may substitute the
  // three-method backend. Gated on localhost, so it is inert in any real
  // deployment, and nothing else in this file depends on it.
  // ══════════════════════════════════════════════════════════════════
  function testBackend() {
    try {
      if (location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') return null;
      return window.__opChatBackend || null;
    } catch (e) { return null; }
  }

  const store = new ChatStore({ backend: testBackend() || idbBackend, user: OP.userId || 'anon' });

  // ═════════════════════════════════════════════════════════════════
  // Elements
  // ═════════════════════════════════════════════════════════════════
  const chatBtn = document.getElementById('ex-chat-btn');
  const chatCount = document.getElementById('ex-chat-count');
  const threadsEl = document.getElementById('ex-chat-threads');
  const threadEl = document.getElementById('ex-chat-thread');
  const inputEl = document.getElementById('ex-chat-input');
  const sendEl = document.getElementById('ex-chat-send');
  const chipsEl = document.getElementById('ex-chat-chips');
  const statusEl = document.getElementById('ex-chat-status');
  const navEl = document.getElementById('ex-chat-nav');
  const backEl = document.getElementById('ex-chat-back');
  const prevEl = document.getElementById('ex-chat-prev');
  const nextEl = document.getElementById('ex-chat-next');
  const recalcEl = document.getElementById('ex-chat-recalc');

  // Screen A is these four sections; the chat replaces all of them at once so
  // there is never half of one screen on top of the other.
  const SCREEN_A = ['ex-reports', 'ex-ask', 'ex-answer', 'ex-rows']
    .map(id => document.getElementById(id)).filter(Boolean);

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === 'text') { n.textContent = attrs[k]; continue; }
      if (k === 'class') n.className = attrs[k];
      else if (k in n && k !== 'for') n[k] = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(c => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }

  // ═════════════════════════════════════════════════════════════════
  // Which thread we are in
  // ═════════════════════════════════════════════════════════════════
  // A thread is per REPORT: the pinned report card if there is one, otherwise
  // the generator the last answer came from, otherwise a single catch-all so a
  // catalogue-only session still has somewhere to put its answers.
  let openReport = null;
  let openN = null;                      // the record screen A is showing
  // P3b: which CONVERSATION of a report is open ("New conversation" starts a
  // second one). Per base report, in memory: a reload lands on the newest
  // thread of that report, which is what the thread list shows anyway.
  const convByBase = new Map();

  function baseReport() {
    const st = OP.state || {};
    if (st.reportGen != null) return baseOf(st.reportGen);
    const oa = st.openAnswer;
    if (oa && oa.intent && oa.intent.generator_id != null) return baseOf(oa.intent.generator_id);
    if (openReport) return baseOf(openReport);
    return 'catalog';
  }

  function currentReport() {
    const base = baseReport();
    return convKey(base, convByBase.get(base) || null);
  }

  // `convByBase` lives in memory only, so F5 used to drop the reader back
  // into the FIRST conversation of the report — and the next question landed
  // in the thread "New conversation" had just closed. The open conversation is
  // therefore RE-DERIVED at boot from the store itself: the newest thread of
  // this report is the one that was being used.
  //
  // Not sessionStorage: two tabs on the same report would write one key over
  // each other, and the store already holds the same truth for both — and
  // holds it after the tab is closed, which is when a person comes back.
  async function restoreConversation() {
    const base = baseReport();
    let threads = [];
    try { threads = await store.threads(); } catch (e) { return; }
    const mine = threads.filter(tv => baseOf(tv.report_id) === base);
    if (!mine.length) return;
    const top = mine.reduce((m, tv) => (tv.last.ts > m.last.ts ? tv : m), mine[0]);
    const conv = convOf(top.report_id);
    if (conv) convByBase.set(base, conv); else convByBase.delete(base);
    if (!openReport || baseOf(openReport) === base) openReport = currentReport();
  }

  // A thread is keyed by generator (plus the conversation); people know the
  // REPORT's title, so that is what the head and the thread list say.
  function reportTitle(rid) {
    const base = baseOf(rid);
    if (base === 'catalog') return t('Catalogue');
    const rep = (OP.reports || []).find(r => String(r.generator_id) === base);
    if (rep && rep.title) return rep.title;
    const gen = (OP.readySemantics() || []).find(g => String(g.generator_id) === base);
    if (gen) return gen.generator;
    return t('Report');
  }

  function genOf(rid) {
    const base = baseOf(rid);
    return (OP.readySemantics() || []).find(g => String(g.generator_id) === base) || null;
  }

  // ═════════════════════════════════════════════════════════════════
  // Recording: one 'answer' event -> one record
  // ═════════════════════════════════════════════════════════════════
  // Two 'answer' events for ONE question (the follow-up branch and the main
  // path can both emit — plan, risk 5). The key is what the plan names:
  // question + parent + regen. The window is short on purpose: it is cleared
  // when the NEXT question starts, and expires by itself so a question asked
  // again five minutes later is a question, not a duplicate.
  const ANSWER_DEDUP_MS = 1500;
  const answerSeen = new Map();
  function seenAnswer(ev) {
    if (ev && ev.regen === true) return false;   // a re-run is meant to land
    const now = Date.now();
    answerSeen.forEach((ts, k) => { if (now - ts > ANSWER_DEDUP_MS) answerSeen.delete(k); });
    const key = answerKey(ev);
    if (answerSeen.has(key)) return true;
    answerSeen.set(key, now);
    return false;
  }

  OP.on('answer', (ev) => {
    // The duplicate is dropped BEFORE the pending card is finished and before
    // anything is written: one record, one `op_chat_count`.
    if (seenAnswer(ev)) return;
    const rid = currentReport();
    // The answer is HERE: "Cancel" has nothing left to stop, and a click
    // that lands between this event and the record below would otherwise
    // cancel the next question instead of this one.
    if (pend && !pend.cancelled) pend.finish();
    const payload = Object.assign({}, ev, {
      provenance: OP.answerProvenance ? OP.answerProvenance() : {},
      fixed_kpi: ev.fixed_kpi != null ? ev.fixed_kpi : pendingFixedKpi,
    });
    if (verifyNote) { payload.notes = (ev.notes || []).concat([verifyNote]); verifyNote = null; }
    // The clarify chip's own words ("without the period filter") belong to the
    // card, not to the question: the question goes to the server verbatim.
    if (clarifyNote) { payload.notes = (payload.notes || []).concat([clarifyNote]); clarifyNote = null; }
    store.add(rid, payload).then(async (rec) => {
      openReport = rid;
      openN = rec.n;
      // "Regenerate" marks the answer it replaced ONLY now, when the
      // replacement exists. Marking it before the question would hide the
      // old card (`.is-old .ex-bub-a` is display:none) with nothing to show
      // instead whenever the new answer failed or was cancelled.
      if (payload.regen && payload.parent_n != null) {
        try { await store.patch(rid, payload.parent_n, { superseded: true }); }
        catch (e) { /* the mark is cosmetic */ }
      }
      setCount();
      if (!chatSection.hidden) renderThread();
    }).catch(() => { /* history is a convenience, never a blocker */ });
  });

  function setCount() {
    store.count(currentReport()).then(n => {
      if (chatCount) chatCount.textContent = String(n);
    }).catch(() => {});
    postBaseCount(currentReport());
  }

  // The pane's own header counts the OPEN conversation (that is the thread
  // on screen); the badge the viewer shows counts the REPORT — see
  // `ChatStore.countBase`. Only in the frame: outside it nobody reads the
  // number, so nobody pays for the extra store pass.
  function postBaseCount(rid) {
    if (!EMBED) return;
    store.countBase(baseOf(rid)).then(n => postCount(n)).catch(() => {});
  }

  // ═════════════════════════════════════════════════════════════════
  // Bubbles — plain DOM, textContent for every string that came from a
  // person or a model. Nothing in this file writes innerHTML.
  // ═════════════════════════════════════════════════════════════════
  // The pane is a panel now, not a 440 px strip: a chart of three bars was
  // a teaser for a grid nobody could reach. Eight is what fits without the
  // card outgrowing the thread.
  const BUB_ROWS = 8;

  function numOf(v) { return OP.fmtNum ? OP.fmtNum(v) : String(v == null ? '' : v); }
  // A metric's value in the metric's own terms: a ratio as a percentage,
  // a volume with its unit (the report says «mln st», so do we).
  function valOf(v, kpi) { return OP.fmtValue ? OP.fmtValue(v, kpi) : numOf(v); }

  function bubbleBodyCore(rec) {
    const box = el('div', { class: 'ex-bub-body' });
    if (rec.kind === 'why' && rec.why) {
      const w = rec.why;
      box.appendChild(el('div', { class: 'ex-bub-line',
        text: w.label + (w.period ? t(' for {p1}', {p1: w.period}) : '') + ' = ' + valOf(w.current, rec.kpi) }));
      box.appendChild(el('div', { class: 'ex-bub-line',
        text: w.shifted ? t('previous period: {p1} · Δ {p2}', {p1: valOf(w.previous, rec.kpi), p2: String(w.delta)})
                        : t('no period specified — nothing to compare with') }));
      box.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim',
        text: rec.rows.length ? t('where it\'s concentrated: {p1} rows', {p1: rec.rows.length})
                              : t('no dimensions') }));
      return box;
    }
    if (rec.kind === 'error') {
      box.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim', text: t('No answer.') }));
      return box;
    }
    const rows = rec.rows || [];
    if (rec.kind === 'trend' && rows.length && rows[0].length >= 4) {
      // [cut, first, last, delta] — worst first, as the page sorted them.
      rows.slice(0, BUB_ROWS).forEach(r => {
        const d = r[3];
        const line = el('div', { class: 'ex-bub-line' + (d != null && d < 0 ? ' ex-bub-down' : ''),
          text: String(r[0]) + ': ' + valOf(r[1], rec.kpi) + ' → ' + valOf(r[2], rec.kpi) + (d == null ? '' : ' (' + (d > 0 ? '+' : '') + valOf(d, rec.kpi) + ')') });
        box.appendChild(line);
      });
      const more = rows.length - Math.min(rows.length, BUB_ROWS);
      if (more > 0) box.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim', text: t('… {p1} more', {p1: more}) }));
      return box;
    }
    if (!rows.length) {
      box.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim', text: t('Nothing found') }));
      return box;
    }
    const vi = valueIndex(rec);
    const li = labelIndex(rec, vi);
    if (rows.length === 1) {
      const v0 = rows[0][vi];
      if (v0 == null || v0 === '') {
        // An aggregate over zero rows: SUM/AVG of nothing is NULL, and an
        // empty number reads as «the chat broke». Say what happened instead.
        box.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim',
          text: t('No data for this slice: the report has no rows matching these filters. Remove the period filter or ask differently.') }));
        return box;
      }
      box.appendChild(el('div', { class: 'ex-bub-num', text: valOf(v0, rec.kpi) }));
      const lbl = rowLabel(rec, rows[0], vi);
      if (lbl) box.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim', text: lbl }));
      return box;
    }
    // Mini-bars: pure divs, the width a computed percentage — never a string
    // that came from anywhere but this function's own arithmetic. Bars scale
    // by magnitude, so a shortfall (negative) is as visible as a volume.
    const nums = rows.slice(0, BUB_ROWS).map(r => Number(r[vi]));
    const max = nums.reduce((m, v) => (isFinite(v) && Math.abs(v) > m ? Math.abs(v) : m), 0);
    rows.slice(0, BUB_ROWS).forEach((r, i) => {
      const line = el('div', { class: 'ex-bub-bar-row' });
      line.appendChild(el('span', { class: 'ex-bub-bar-label', text: rowLabel(rec, r, vi) }));
      const track = el('span', { class: 'ex-bub-bar-track' });
      const fill = el('span', { class: 'ex-bub-bar-fill' + (isFinite(nums[i]) && nums[i] < 0 ? ' is-neg' : '') });
      const pct = max > 0 && isFinite(nums[i]) ? Math.max(0, Math.min(100, (Math.abs(nums[i]) / max) * 100)) : 0;
      fill.style.width = pct.toFixed(1) + '%';
      track.appendChild(fill);
      line.appendChild(track);
      line.appendChild(el('span', { class: 'ex-bub-bar-num', text: valOf(r[vi], rec.kpi) }));
      box.appendChild(line);
    });
    const more = (rec.total != null ? rec.total : rows.length) - Math.min(rows.length, BUB_ROWS);
    if (more > 0) box.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim', text: t('… {p1} more', {p1: more}) }));
    return box;
  }

  // "Compare" produces a record whose parent is the one it was compared
  // against — so the two numbers stand side by side without either record
  // storing anything the other already has.
  function compareLine(rec, parent) {
    if (rec.source !== 'compare' || !parent) return null;
    const a = (parent.rows || [])[0], b = (rec.rows || [])[0];
    if (!a || !b) return null;
    const va = Number(a[valueIndex(parent)]), vb = Number(b[valueIndex(rec)]);
    if (!isFinite(va) || !isFinite(vb)) return null;
    const d = vb - va;
    return el('div', { class: 'ex-bub-line ex-bub-compare',
      text: t('was {p1} → now {p2} · Δ {p3}{p4}', {p1: numOf(va), p2: numOf(vb), p3: (d > 0 ? '+' : ''), p4: numOf(d)}) });
  }

  function fmtTime(ms) {
    const d = new Date(ms);
    if (isNaN(d.getTime())) return '';
    return OP_I18N.fmtDate(d, { hour: '2-digit', minute: '2-digit',
      day: '2-digit', month: '2-digit' });
  }

  // ── SVG, not emoji ────────────────────────────────────────────────────
  // 👍/👎 as a font glyph renders as a yellow cartoon hand on Windows and as
  // nothing at all in some corporate builds. Two paths, currentColor, done.
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const THUMB_UP = 'M7 11v9M7 11l4-8a2 2 0 013 2v4h5a2 2 0 012 2l-1.5 7a2 2 0 01-2 2H7M3 11h4v9H3z';
  const THUMB_DOWN = 'M17 13V4M17 13l-4 8a2 2 0 01-3-2v-4H5a2 2 0 01-2-2l1.5-7a2 2 0 012-2H17M21 13h-4V4h4z';
  function svgIcon(d) {
    const mk = (tag) => (document.createElementNS
      ? document.createElementNS(SVG_NS, tag) : document.createElement(tag));
    const svg = mk('svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = mk('path');
    path.setAttribute('d', d);
    svg.appendChild(path);
    return svg;
  }

  // ── the pane's chrome (P3a) ───────────────────────────────────────────
  // Everything here reads what the page ALREADY holds: the coverage pill is
  // `state.indexStats` + `state.workerTables` (what the last index left
  // behind), the width class is the number the pane posts down. Neither
  // costs a request — an index is not re-read to draw a pill.
  const covEl = document.getElementById('ex-chat-cov');
  const covTextEl = document.getElementById('ex-chat-cov-text');
  function coverageText() {
    const st = OP.state || {};
    return coverageOf(st.indexStats, st.workerTables, (n) => OP_I18N.fmtNum(n));
  }
  // The partition keys behind that pill — the same already-loaded index, read
  // for the H10 hint ("the question's period is not in the index"). No request.
  function coverageKeys() {
    const out = [];
    ((OP.state || {}).workerTables || []).forEach(tv => (tv.partitions || []).forEach(pt => {
      const k = pt && pt.key != null ? String(pt.key) : '';
      if (k && out.indexOf(k) < 0) out.push(k);
    }));
    return out;
  }
  function covlineText(cov) {
    return cov ? t('in the index: {p1}', { p1: cov })
      : EMBED ? t('the index is still loading…') : t('there is no index yet — build it with the button above');
  }
  function syncCoverage() {
    const txt = coverageText();
    // The intro's own coverage line was drawn when the thread was empty —
    // usually before the index arrived — and nothing redrew it, so the pane
    // kept saying "index still loading…" under a pill that already
    // counted 36 partitions (demo stand 2026-09-07). Same timer, same text.
    const intro = threadEl && threadEl.querySelector ? threadEl.querySelector('.ex-chat-intro') : null;
    const line = intro ? intro.querySelector('.ex-chat-covline') : null;
    if (line) line.textContent = covlineText(txt);
    if (!covEl) return;
    if (covTextEl) covTextEl.textContent = txt;
    // No coverage = no pill: an empty pill said nothing, and the folder pill
    // beside it is the one that names the missing step.
    covEl.hidden = !txt;
  }

  // The pane owns its width and posts the number down (`op_chat_width`).
  // Below 400 px the long placeholder wraps and the chrome starts eating the
  // thread, so the frame shortens one and folds the other.
  const NARROW_PX = 400;
  let paneWide = false;
  const wideBtn = document.getElementById('ex-chat-wide');
  function applyPaneWidth(d) {
    const w = Number(d && d.w) || 0;
    paneWide = !!(d && d.wide);
    const narrow = w > 0 && w < NARROW_PX;
    if (chatSection.classList) {
      chatSection.classList.toggle('is-narrow', narrow);
      chatSection.classList.toggle('is-wide', paneWide);
    }
    if (inputEl) inputEl.placeholder = narrow ? t('Question about the report…') : t('Ask about report figures…');
    // The metric chips are already ONE scrolling line (.ex-chat-chips is
    // nowrap + overflow-x), so a narrow pane needs no second rule for them —
    // what it needs is the shorter placeholder above and the folded chrome
    // the `is-narrow` class does in CSS.
    if (wideBtn) wideBtn.setAttribute('aria-pressed', paneWide ? 'true' : 'false');
  }
  wideBtn && wideBtn.addEventListener('click', () => {
    paneWide = !paneWide;
    wideBtn.setAttribute('aria-pressed', paneWide ? 'true' : 'false');
    postToPane({ type: 'op_chat_wide', wide: paneWide });
  });
  const closeBtn = document.getElementById('ex-chat-close');
  closeBtn && closeBtn.addEventListener('click', () => postToPane({ type: 'op_chat_close' }));
  // Outside the pane there is no parent to ask: both geometry buttons would
  // do nothing at all, so they are not on screen.
  if (!EMBED) {
    if (wideBtn) wideBtn.hidden = true;
    if (closeBtn) closeBtn.hidden = true;
  }

  // "⋯": four items, all of them real. "Clear chat" is the control that
  // already exists, moved into the menu for the pane; the other three are
  // P3b — a new conversation, the conversations of this report, and the
  // thread as text.
  const moreBtn = document.getElementById('ex-chat-more');
  const menuEl = document.getElementById('ex-chat-menu');
  function openMenu(on) {
    if (!menuEl) return;
    menuEl.hidden = !on;
    if (moreBtn) moreBtn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  moreBtn && moreBtn.addEventListener('click', () => openMenu(!!(menuEl && menuEl.hidden)));
  // A menu that only closes by picking something is a trap: a click anywhere
  // else and Escape both close it. Escape is handled here rather than in the
  // pane's own Escape (which closes the WHOLE pane) — that one steps aside
  // while the menu is open, see `menuOpen()` there.
  function menuOpen() { return !!(menuEl && !menuEl.hidden); }
  function inMenu(node) {
    let n = node;
    for (let i = 0; i < 12 && n; i++) {
      if (n === menuEl || n === moreBtn) return true;
      n = n.parentNode || n.parent || null;
    }
    return false;
  }
  document.addEventListener('click', (e) => {
    if (!menuOpen()) return;
    if (e && inMenu(e.target)) return;
    openMenu(false);
  });
  document.addEventListener('keydown', (e) => {
    if (!e || e.key !== 'Escape' || !menuOpen()) return;
    openMenu(false);
    if (moreBtn && moreBtn.focus) { try { moreBtn.focus(); } catch (err) { /* stub DOM */ } }
  });
  if (menuEl) {
    Array.prototype.forEach.call(menuEl.querySelectorAll('[data-m]'), (b) => {
      b.addEventListener('click', () => {
        openMenu(false);
        const m = b.getAttribute('data-m');
        if (m === 'new') { newConversation(); return; }
        if (m === 'hist') { showHistory(true); return; }
        if (m === 'copy') { copyThread(); return; }
        const clear = document.getElementById('ex-chat-clear');
        if (m === 'clear' && clear) {
          if (typeof clear.click === 'function') clear.click();
          else clear.dispatchEvent({ type: 'click', target: clear });
          return;
        }
      });
    });
  }

  // ── the answer card (P3a) ─────────────────────────────────────────────
  // The order is the order a reader needs it in: what was asked, how sure
  // the machine is, what it understood, the number, how it was computed,
  // and only then what to do about it.

  function kpiDimsOf(rec) {
    const gen = genOf(rec.report_id || currentReport());
    const key = (rec.intent && rec.intent.kpi) || (rec.kpi && rec.kpi.key);
    if (!gen || !key || !OP.kpiOf) return [];
    const k = OP.kpiOf(gen, key) || {};
    return k.dims || [];
  }
  function kpiLabel(key) {
    const gen = genOf(currentReport());
    const k = gen && OP.kpiOf ? (OP.kpiOf(gen, key) || {}) : {};
    return k.short || k.name || String(key);
  }

  // The title of the card: the metric and its cut, in words. Not the
  // question — the question is already above it, in the operator's own words.
  function cardTitle(rec) {
    const k = rec.kpi || {};
    const it = rec.intent || {};
    const name = k.short || k.name || it.kpi || (rec.kind === 'sql' ? t('Query from semantics') : t('Answer'));
    const cuts = (it.dims || []).map(String).filter(d => !/^(year|month|week|day|quarter)$/.test(d));
    const per = (it.filters || []).filter(f => f && /^(year|month|week|day|quarter)$/.test(String(f.dim)))
      .map(f => (f.value != null ? String(f.value) : (f.values || []).join(','))).filter(Boolean);
    // Every shape is ONE key with placeholders: a translator gets a whole
    // phrase, not "Trend: " + " by " + a comma, which no language survives.
    let out = String(name);
    const cut = cuts.join(', ');
    const period = per.join(' ');
    if (cut && period) out = t('{p1} by {p2} for {p3}', {p1: out, p2: cut, p3: period});
    else if (cut) out = t('{p1} by {p2}', {p1: out, p2: cut});
    else if (period) out = t('{p1} for {p2}', {p1: out, p2: period});
    if (rec.kind === 'why') out = t('Why: {p1}', {p1: out});
    if (rec.kind === 'trend') out = t('Trend: {p1}', {p1: out});
    return out;
  }

  // The table half of the Chart|Table segment: the record's own rows, in
  // the metric's own units, at most 30 — the grid lives behind "Expand".
  function bubbleTable(rec) {
    const wrap = el('div', { class: 'ex-bub-tablewrap' });
    const tbl = el('table', { class: 'ex-bub-table' });
    const vi = valueIndex(rec);
    const head = el('tr');
    (rec.cols || []).forEach((c, i) => head.appendChild(el('th', { text: String(c), class: i === vi ? 'n' : '' })));
    tbl.appendChild(head);
    (rec.rows || []).slice(0, 30).forEach(row => {
      const tr = el('tr');
      (rec.cols || []).forEach((c, i) => {
        const raw = row[i];
        tr.appendChild(el('td', { class: i === vi ? 'n' : '',
          text: i === vi ? valOf(raw, rec.kpi) : String(raw == null ? '' : raw) }));
      });
      tbl.appendChild(tr);
    });
    wrap.appendChild(tbl);
    return wrap;
  }

  // ── "Show in report" ───────────────────────────────────────────────
  // The chat frame cannot reach the sandboxed report window; viewer.js is
  // the bridge (see .claude/skills/dashboard-protocols/SKILL.md, «Locate»).
  // Every request carries an id and is answered exactly once — with
  // {ok:false} when the report has no such place, which the card SAYS.
  const locPending = new Map();
  let locSeq = 0;
  // A parent that never answers is the one case the protocol cannot see: no
  // message ever arrives, so the button would stay disabled and the line
  // would say "searching…" for the rest of the session.
  const LOC_TIMEOUT_MS = Number((window.__opChatLocTimeout != null) ? window.__opChatLocTimeout : 2000);
  function locateSettle(id, d) {
    const ent = locPending.get(String(id));
    if (!ent) return;
    locPending.delete(String(id));
    if (ent.timer) { try { clearTimeout(ent.timer); } catch (e) { /* stub DOM */ } }
    ent.fn(d);
  }
  function locateAnswer(d) { locateSettle(d.id, d); }
  if (EMBED && window.addEventListener) {
    window.addEventListener('message', (e) => {
      try { if (e.origin !== location.origin) return; } catch (err) { return; }
      const d = e.data;
      if (!d) return;
      if (d.type === 'op_chat_located') { locateAnswer(d); return; }
      if (d.type === 'op_chat_width') { applyPaneWidth(d); return; }
    });
  }
  function locateRecord(rec, msgEl, btn) {
    if (!EMBED) return;
    const vi = valueIndex(rec);
    const row = (rec.rows || [])[0] || [];
    // Context, not data: the filter VALUES the question named («Magnit»,
    // week 23 → also «W23», the way reports print weeks) and the row labels.
    // The report searches its own text for them, so the cell that answers
    // is the one whose row and column carry this context — a bare number
    // («100,0%») is in a dozen cells and proves nothing (owner, 2026-09-07).
    const ctxLabels = [];
    ((rec.intent && rec.intent.filters) || []).forEach(f => {
      if (!f) return;
      const vals = f.value != null ? [f.value] : (Array.isArray(f.values) ? f.values : []);
      vals.slice(0, 4).forEach(v => {
        const sv = String(v == null ? '' : v).trim();
        if (!sv) return;
        ctxLabels.push(sv);
        if (/week/i.test(String(f.dim || ''))) ctxLabels.push('W' + sv);
      });
    });
    const labels = (rec.rows || []).slice(0, 8)
      .map(r => rowLabel(rec, r, vi)).filter(Boolean).concat(ctxLabels).slice(0, 20);
    const id = 'loc' + (++locSeq) + '-' + rec.n;
    msgEl.textContent = t('searching the report…');
    btn.disabled = true;
    const entry = { fn: (d) => {
      btn.disabled = false;
      if (d && d.ok) { msgEl.textContent = ''; return; }
      // The honest line the protocol exists for: the button must never
      // pretend it scrolled somewhere.
      msgEl.textContent = (d && d.reason === 'timeout')
        ? t('the report did not answer — try again')
        : t('not found in the report');
    }, timer: null };
    locPending.set(id, entry);
    entry.timer = setTimeout(() => locateSettle(id, { ok: false, reason: 'timeout' }), LOC_TIMEOUT_MS);
    if (entry.timer && typeof entry.timer.unref === 'function') entry.timer.unref();   // node harness
    postToPane({ type: 'op_chat_locate', id: id, q: rec.question,
      kpi: (rec.kpi && (rec.kpi.short || rec.kpi.name || rec.kpi.key)) || (rec.intent && rec.intent.kpi) || '',
      value: row.length ? valOf(row[vi], rec.kpi) : '', labels: labels });
  }

  // ── the clarify card: what stands INSTEAD of an empty answer ──────────
  function clarifyCard(rec) {
    const box = el('div', { class: 'ex-bub-ask' });
    const reason = clarifyReason(rec);
    box.appendChild(el('div', { class: 'ex-bub-ask-t',
      text: reason === 'null'
        ? t('No rows were found for this slice — there was nothing to calculate.')
        : t('There isn\'t a single row in the index for this question.') }));
    const cov = coverageText();
    box.appendChild(el('div', { class: 'ex-bub-ask-d',
      text: cov ? t('In the index: {p1}. What should be calculated instead?', {p1: cov})
                : t('What should be calculated instead?') }));
    const lowWhy = lowConfidence(rec);
    if (lowWhy) {
      box.appendChild(el('div', { class: 'ex-bub-ask-d',
        text: lowWhy === 'match'
          ? t('The question matched two metrics almost equally — a different one may have been meant.')
          : t('The question didn\'t match the metric exactly — check "Understood as".') }));
    }
    // H10 — the emptiness has a name: the period asked for is not in the index
    // this browser holds. Both halves of the sentence come from what is
    // already loaded; nothing is fetched to say it.
    if (reason === 'empty' && periodOutsideCoverage(rec, coverageKeys())) {
      box.appendChild(el('div', { class: 'ex-bub-ask-d ex-bub-ask-cov',
        text: cov ? t('In the index: {p1}; the question\'s period isn\'t in it — remove the period filter?', {p1: cov})
                  : t('The question\'s period isn\'t in the index — remove the period filter?') }));
    }
    // A cut the metric does not have: say which ones it DOES have, as chips
    // that re-run the same question by one of them.
    const dims = kpiDimsOf(rec);
    const bad = unavailableDims(rec, dims);
    // A period the metric is not cut by is not a missing CUT: the question
    // filtered by it, the engine dropped the filter, and "not cut by
    // 'week'. Available: client, sklad" read as an answer to a question
    // nobody asked. Two sentences, because they are two facts.
    const badTime = bad.filter(d => TIME_DIM_RE.test(String(d)));
    const badCut = bad.filter(d => !TIME_DIM_RE.test(String(d)));
    if (badTime.length) {
      box.appendChild(el('div', { class: 'ex-bub-ask-d ex-bub-ask-per',
        text: t('The metric has no cut for period "{p1}" — calculated without it.',
          {p1: badTime.join(', ')}) }));
    }
    if (badCut.length) {
      box.appendChild(el('div', { class: 'ex-bub-ask-d ex-bub-ask-dim',
        text: t('The metric can\'t be sliced by "{p1}". Available: {p2}', {p1: badCut.join(', '), p2: dims.join(', ')}) }));
      const row = el('div', { class: 'ex-bub-opts ex-bub-dims' });
      dims.slice(0, 4).forEach(d => {
        const b = el('button', { type: 'button', class: 'ex-bub-opt',
          'data-clarify': 'usedim', 'data-dim': String(d) });
        b.appendChild(el('span', { text: t('by {p1}', {p1: String(d)}) }));
        b.appendChild(el('span', { class: 'k', text: t('this cut is available') }));
        b.addEventListener('click', () => runClarify(rec, { act: 'usedim', arg: String(d) }));
        row.appendChild(b);
      });
      box.appendChild(row);
    }
    const opts = el('div', { class: 'ex-bub-opts' });
    clarifyOptions(rec).forEach(op => {
      let label = '';
      if (op.act === 'allperiod') label = t('for the whole period');
      else if (op.act === 'nofilter') label = t('without a filter by {p1}', {p1: op.arg});
      else if (op.act === 'alt') label = kpiLabel(op.arg);
      else label = t('Ask differently');
      const b = el('button', { type: 'button', class: 'ex-bub-opt', 'data-clarify': op.act });
      b.appendChild(el('span', { text: label }));
      b.appendChild(el('span', { class: 'k', text: op.hint || '' }));
      b.addEventListener('click', () => runClarify(rec, op));
      opts.appendChild(b);
    });
    box.appendChild(opts);
    return box;
  }

  // Every option is a real, deterministic re-run: the SAME intent with one
  // thing removed or one metric swapped, straight through finishIntent.
  let clarifyNote = null;
  async function runClarify(rec, op) {
    if (op.act === 'ask') { if (inputEl && inputEl.focus) inputEl.focus(); return; }
    const gen = await genReady(rec);
    if (!gen) return;
    const base = Object.assign({}, rec.intent || {});
    let note = '';
    if (op.act === 'allperiod') {
      base.filters = (base.filters || []).filter(f => !/^(year|month|week|day|quarter)$/.test(String(f.dim)));
      note = t('without a period filter');
    } else if (op.act === 'nofilter') {
      base.filters = (base.filters || []).filter(f => String(f.dim) !== String(op.arg));
      note = t('without a filter by {p1}', {p1: op.arg});
    } else if (op.act === 'alt') {
      base.kpi = String(op.arg);
      note = t('a different metric: {p1}', {p1: kpiLabel(op.arg)});
    } else if (op.act === 'usedim') {
      // The cut the metric really has, and the filters it can actually apply:
      // keeping a filter by a dim the KPI does not know would empty the answer
      // a second time, for the same reason.
      const have = kpiDimsOf(rec).map(String);
      base.dims = [String(op.arg)];
      base.filters = (base.filters || []).filter(f => have.indexOf(String(f.dim)) >= 0);
      note = t('cut: {p1}', {p1: String(op.arg)});
    }
    // The question goes to the engine EXACTLY as the operator typed it: the
    // note is translated UI text, and gluing it on shipped a Russian tail to
    // the server (and into the record) for every locale. It rides as its own
    // field instead and lands on the card as a note.
    OP.state.askQuestion = rec.question;
    clarifyNote = note || null;
    OP.state.askSource = 'clarify';
    OP.state.askParentN = rec.n;
    await OP.finishIntent(OP.withGen(base, gen), gen, note);
  }

  // ── votes ─────────────────────────────────────────────────────────────
  // The vote is local FIRST (the button flips and the record keeps it) and
  // only then posted: the network is not allowed to hold up a click, and a
  // failed post leaves the vote exactly where a pre-P4 vote lived — in this
  // browser. Without `answer_id` (the server recorded nothing for this
  // answer) nothing is posted at all.
  async function setVote(rec, vote, up, down) {
    const next = rec.vote === vote ? null : vote;
    rec.vote = next;
    up.classList.toggle('is-on', next === 'up');
    down.classList.toggle('is-on', next === 'down');
    try { await store.patch(rec.report_id || currentReport(), rec.n, { vote: next }); }
    catch (e) { /* a vote is not worth a broken thread */ }
    setStatus(next === 'down'
      ? t('Recorded: the answer is incorrect. This will go to the report\'s author in the next update.')
      : next === 'up' ? t('Recorded: the answer is correct.') : '');
    postVote(rec, next);
  }

  // POST …/vote — fire-and-forget. Only the id and the verdict travel: no
  // question, no SQL, no row. `same-origin` credentials + the JSON
  // Content-Type are what OriginGuard requires of a state-changing call.
  async function postVote(rec, vote) {
    if (!rec || rec.answer_id == null) return;
    try {
      const r = await fetch('/api/explore/answers/' + encodeURIComponent(String(rec.answer_id)) + '/vote', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vote: vote }),
      });
      if (!r.ok) return;
      const out = await r.json().catch(() => null);
      if (out && out.suggested) setStatus(t('Thanks, the form suggestion has been recorded.'));
    } catch (e) { /* offline: the vote stays local, exactly as before P4 */ }
  }

  // ── "Regenerate" ────────────────────────────────────────────────
  // The same question, asked again on purpose: the cache/dedup is bypassed
  // (`askRegen`), the new answer becomes the card, and this one goes to
  // history as one quiet line instead of two cards saying the same thing.
  async function regenerate(rec, btn) {
    btn.disabled = true;
    // The «superseded» mark is set by the 'answer' handler, once the new
    // version exists — see OP.on('answer'). A regenerate that fails must
    // leave the answer it was going to replace on screen.
    if (OP.state) { OP.state.askRegen = true; OP.state.askParentN = rec.n; }
    // The parent rides in the options too: `runQuestion` sets `askParentN`
    // itself, and without this the replacement would hang off whatever card
    // screen A had open instead of the one "Regenerate" was pressed on.
    // NOT the intent: a regenerate is the whole question asked again, so it
    // must take the same route it took the first time, not become a
    // deterministic follow-up to its own answer.
    // …and with the metric the reader pinned. A regenerate is the same
    // question asked again — including the answer to "which metric?".
    await sendQuestion(rec.question, { regen: true, parentN: rec.n,
      fixedKpi: rec.fixed_kpi || null });
    // Adversarial P3b, reviewed and left as is: the button is released when
    // the QUESTION is answered, which is a tick or two before the record is
    // written (the 'answer' handler writes it). Holding it until the write
    // would buy nothing — a second press is a `regen` with the same key as
    // the first only while that first one is still in flight, and
    // `sendQuestion` already returns the very same promise for it.
    btn.disabled = false;
  }

  // ── "Clarify" (P3b) ──────────────────────────────────────────────────
  // "Not that" said in the order a person needs it: my own words first, then the
  // metrics the router itself shortlisted, then "just not this metric",
  // then the filters THIS answer carried. Every one of them hangs off THIS
  // card — the parent of what comes back is this record, never the newest one
  // in the thread — and none of them costs a request to draw.
  function kpiNodeOf(rec, key) {
    const gen = genOf((rec && rec.report_id) || currentReport());
    if (!gen || !OP.kpiOf) return null;
    return OP.kpiOf(gen, key) || null;
  }

  // The alternatives, as buttons: the label the report gives the metric and
  // how well it fits the question, in per cent. Shared by the "Clarify"
  // panel and the low-confidence line, so the two can never disagree.
  function altButtons(rec) {
    return shortlistAlts(rec, (key) => kpiNodeOf(rec, key)).map(a => {
      const b = el('button', { type: 'button', class: 'ex-bub-opt',
        'data-chat-act': 'refine-alt', 'data-kpi': a.key });
      b.appendChild(el('span', { text: kpiLabel(a.key) }));
      b.appendChild(el('span', { class: 'k', text: t('match {p1}%', {p1: a.pct}) }));
      b.addEventListener('click', () => sendQuestion(rec.question,
        { parentN: rec.n, parentIntent: rec.intent, fixedKpi: a.key }));
      return b;
    });
  }

  function refinePanel(rec) {
    const box = el('div', { class: 'ex-bub-refine', 'data-refine': String(rec.n) });
    // (a) free text, first: a person who already knows what they meant should
    // not have to read four buttons to say it.
    const row = el('div', { class: 'ex-refine-row' });
    const inp = el('input', { type: 'text', class: 'ex-refine-in',
      'data-chat-act': 'refine-text', placeholder: t('Refine the question…'),
      'aria-label': t('Refine the question') });
    const go = el('button', { type: 'button', class: 'ex-refine-go',
      'data-chat-act': 'refine-send', text: t('Refine') });
    const send = () => {
      const v = String(inp.value || '').trim();
      if (!v) { if (inp.focus) { try { inp.focus(); } catch (e) { /* stub DOM */ } } return; }
      inp.value = '';
      sendQuestion(v, { parentN: rec.n, parentIntent: rec.intent });
    };
    go.addEventListener('click', send);
    inp.addEventListener('keydown', (e) => { if (e && e.key === 'Enter') send(); });
    row.appendChild(inp);
    row.appendChild(go);
    box.appendChild(row);

    // (b) the alternatives the router shortlisted.
    const alts = altButtons(rec);
    if (alts.length) {
      box.appendChild(el('div', { class: 'ex-bub-ask-d', text: t('It might be about a different metric:') }));
      const opts = el('div', { class: 'ex-bub-opts' });
      alts.forEach(b => opts.appendChild(b));
      box.appendChild(opts);
    }

    const rest = el('div', { class: 'ex-bub-opts' });
    // (c) "just not this one": the best remaining candidate answers instead. With
    // nothing left to switch to the button SAYS so — it never re-runs the same
    // metric under a different name.
    if (rec.intent && rec.intent.kpi) {
      const not = el('button', { type: 'button', class: 'ex-bub-opt', 'data-chat-act': 'refine-not' });
      not.appendChild(el('span', { text: t('ask again without this metric') }));
      not.appendChild(el('span', { class: 'k', text: kpiLabel(rec.intent.kpi) }));
      not.addEventListener('click', () => {
        const next = shortlistAlts(rec, (key) => kpiNodeOf(rec, key))[0];
        if (!next) { setStatus(t('The report doesn\'t suggest any other metrics for this question.')); return; }
        sendQuestion(rec.question, { parentN: rec.n, parentIntent: rec.intent, fixedKpi: next.key });
      });
      rest.appendChild(not);
    }
    // (d) the cut and the period — the same options the clarify card offers,
    // built by the same function.
    clarifyOptions(rec).filter(o => o.act === 'allperiod' || o.act === 'nofilter').forEach(op => {
      const b = el('button', { type: 'button', class: 'ex-bub-opt',
        'data-chat-act': 'refine-fix', 'data-clarify': op.act });
      b.appendChild(el('span', { text: op.act === 'allperiod'
        ? t('for the whole period') : t('remove the {p1} filter', {p1: op.arg}) }));
      b.appendChild(el('span', { class: 'k', text: op.hint || '' }));
      b.addEventListener('click', () => runClarify(rec, op));
      rest.appendChild(b);
    });
    if (rest.children.length) box.appendChild(rest);
    return box;
  }

  function renderBubble(rec, byN) {
    // One turn: the question (right, accent) + the answer card (left).
    const card = el('div', { class: 'ex-bub' + (rec.superseded ? ' is-old' : ''), 'data-n': String(rec.n) });
    card.appendChild(el('div', { class: 'ex-bub-q', text: rec.question }));
    if (rec.superseded) {
      // Replaced by a "Regenerate": the answer stays in history, but
      // two full cards saying almost the same thing is how a thread becomes
      // unreadable.
      card.appendChild(el('div', { class: 'ex-bub-old-line',
        text: t('this answer was regenerated — the new version is below') }));
      return card;
    }
    const ans = el('div', { class: 'ex-bub-a' });

    // 1 — the badge: who chose the metric, and how surely.
    const badge = tierBadge(rec);
    const hd = el('div', { class: 'ex-bub-hd' });
    hd.appendChild(el('h4', { class: 'ex-bub-title', text: cardTitle(rec) }));
    hd.appendChild(el('span', { class: ('ex-bub-tag ' + badge.cls).trim(), text: badge.text }));
    const rows = rec.rows || [];
    const clarify = clarifyReason(rec);
    let chartBox = null, tableBox = null;
    if (!clarify && rows.length > 1 && rec.kind !== 'why') {
      const seg = el('div', { class: 'ex-bub-seg' });
      const bChart = el('button', { type: 'button', class: 'is-on', text: t('Chart'),
        'data-chat-act': 'chart' });
      const bTable = el('button', { type: 'button', text: t('Table'), 'data-chat-act': 'table' });
      bChart.addEventListener('click', () => {
        bChart.className = 'is-on'; bTable.className = '';
        if (chartBox) chartBox.hidden = false;
        if (tableBox) tableBox.hidden = true;
      });
      bTable.addEventListener('click', () => {
        bTable.className = 'is-on'; bChart.className = '';
        if (chartBox) chartBox.hidden = true;
        if (tableBox) tableBox.hidden = false;
      });
      seg.appendChild(bChart); seg.appendChild(bTable);
      hd.appendChild(seg);
    }
    ans.appendChild(hd);

    // 2 — "understood as": the chips an operator checks before believing.
    const chips = understood(rec);
    if (chips.length) {
      const und = el('div', { class: 'ex-bub-und' });
      und.appendChild(el('span', { text: t('Understood as:') }));
      chips.forEach(([label, value]) => {
        const chip = el('span', { class: 'ex-bub-chip' });
        chip.appendChild(el('span', { text: label + ' ' }));
        chip.appendChild(el('b', { text: String(value) }));
        und.appendChild(chip);
      });
      ans.appendChild(und);
    }

    // 3 — the body: a clarify card when there is no number, else the chart
    //     (and the table behind the segment).
    const cmp = compareLine(rec, byN.get(rec.parent_n));
    if (cmp) { const cw = el('div', { class: 'ex-bub-body' }); cw.appendChild(cmp); ans.appendChild(cw); }
    if (clarify) {
      const body = el('div', { class: 'ex-bub-body' });
      body.appendChild(clarifyCard(rec));
      ans.appendChild(body);
    } else {
      chartBox = bubbleBodyCore(rec);
      ans.appendChild(chartBox);
      if (rows.length > 1 && rec.kind !== 'why') {
        tableBox = bubbleTable(rec);
        tableBox.hidden = true;
        ans.appendChild(tableBox);
      }
    }
    (rec.notes || []).forEach(tv => {
      ans.appendChild(el('div', { class: 'ex-bub-line ex-bub-note', text: tv }));
    });

    // P3b — the number IS shown (hiding it would answer a question nobody
    // asked), and right under it the card admits the reading was close and
    // offers the metrics it was close to.
    const lowWhy2 = clarify ? false : lowConfidence(rec);
    // A model pick with no alternative worth offering is not a warning — it
    // is a line on every model answer this deployment ever gives. The
    // numeric ("almost the same") reason still speaks on its own.
    const alts = lowWhy2 ? altButtons(rec) : [];
    if (lowWhy2 && (lowWhy2 !== 'model' || alts.length)) {
      const low = el('div', { class: 'ex-bub-lowconf' });
      low.appendChild(el('div', { class: 'ex-bub-line ex-bub-dim',
        text: alts.length
          ? t('The question didn\'t match the metric exactly — a different one may have been meant:')
          : t('The question didn\'t match the metric exactly — check "Understood as".') }));
      if (alts.length) {
        const opts = el('div', { class: 'ex-bub-opts' });
        alts.forEach(b => opts.appendChild(b));
        low.appendChild(opts);
      }
      ans.appendChild(low);
    }

    // 4 — the row of verbs.
    const how = el('div', { class: 'ex-bub-how' });
    const sqlPre = rec.sql ? el('pre', { class: 'ex-bub-sql', text: rec.sql }) : null;
    if (sqlPre) {
      sqlPre.hidden = true;
      const howBtn = el('button', { type: 'button', class: 'ex-bub-lnk',
        'data-chat-act': 'how', text: t('How it was calculated') });
      howBtn.addEventListener('click', () => { sqlPre.hidden = !sqlPre.hidden; });
      how.appendChild(howBtn);
    }
    if (EMBED && !clarify && rows.length) {
      const locMsg = el('span', { class: 'ex-bub-locmsg' });
      const locBtn = el('button', { type: 'button', class: 'ex-bub-lnk',
        'data-chat-act': 'locate', text: t('Show in report') });
      locBtn.addEventListener('click', () => locateRecord(rec, locMsg, locBtn));
      how.appendChild(locBtn);
      how.appendChild(locMsg);
    }
    how.appendChild(el('span', { class: 'ex-bub-how-sp' }));
    const up = el('button', { type: 'button', class: 'ex-bub-vote' + (rec.vote === 'up' ? ' is-on' : ''),
      'data-chat-act': 'vote-up', title: t('Answer is correct'), 'aria-label': t('Answer is correct') });
    up.appendChild(svgIcon(THUMB_UP));
    const down = el('button', { type: 'button', class: 'ex-bub-vote' + (rec.vote === 'down' ? ' is-on' : ''),
      'data-chat-act': 'vote-down', title: t('Answer is incorrect'), 'aria-label': t('Answer is incorrect') });
    down.appendChild(svgIcon(THUMB_DOWN));
    up.addEventListener('click', () => setVote(rec, 'up', up, down));
    down.addEventListener('click', () => setVote(rec, 'down', up, down));
    how.appendChild(up);
    how.appendChild(down);
    if (rec.kind !== 'error') {
      const again = el('button', { type: 'button', class: 'ex-bub-lnk',
        'data-chat-act': 'regen', text: t('Regenerate') });
      again.addEventListener('click', () => regenerate(rec, again));
      how.appendChild(again);
    }
    ans.appendChild(how);
    // "Clarify" — the panel opens UNDER this card and is built on the first
    // click: an unopened panel costs nothing, and an opened one costs no
    // request either.
    if (rec.kind !== 'error') {
      const refBox = el('div', { class: 'ex-bub-refine-box' });
      refBox.hidden = true;
      const refBtn = el('button', { type: 'button', class: 'ex-bub-lnk',
        'data-chat-act': 'refine', 'aria-expanded': 'false', text: t('Refine') });
      refBtn.addEventListener('click', () => {
        if (!refBox.children.length) refBox.appendChild(refinePanel(rec));
        refBox.hidden = !refBox.hidden;
        refBtn.setAttribute('aria-expanded', refBox.hidden ? 'false' : 'true');
      });
      how.appendChild(refBtn);
      ans.appendChild(refBox);
    }
    if (sqlPre) ans.appendChild(sqlPre);

    // 5 — follow-ups: the cuts this metric has and this answer did not use.
    if (!clarify && rec.intent && rec.intent.kpi) {
      const sugg = followUps(rec, kpiDimsOf(rec));
      if (sugg.length) {
        const fu = el('div', { class: 'ex-bub-fu' });
        sugg.forEach(qq => {
          const b = el('button', { type: 'button', text: qq, 'data-chat-act': 'followup' });
          b.addEventListener('click', () => { if (inputEl) { inputEl.value = qq; } sendQuestion(qq); });
          fu.appendChild(b);
        });
        ans.appendChild(fu);
      }
    }

    // 6 — the provenance footer: tier · confidence · freshness · owner.
    const meta = [badge.text];
    const conf = confidenceText(rec);
    if (conf) meta.push(conf);
    const p = rec.provenance || {};
    if ((p.partitions_used || []).length) meta.push(t('by {p1} partitions', {p1: p.partitions_used.length}));
    if (p.as_of) meta.push(t('data as of {p1}', {p1: OP_I18N.fmtDate(new Date(p.as_of))}));
    // The owner is optional in the semantics (ADR-13) — a KPI without one
    // simply has no owner line, never "owner: —".
    const owner = (OP.metricOwnerLine && rec.kpi) ? OP.metricOwnerLine(rec.kpi) : '';
    if (owner) meta.push(owner);
    meta.push(fmtTime(rec.ts));

    const ft = el('div', { class: 'ex-bub-ft' });
    card.appendChild(ans);
    ans.appendChild(ft);

    const acts = el('div', { class: 'ex-bub-acts' });
    const expand = el('button', { type: 'button', class: 'btn xs ghost ex-bub-act',
      'data-act': 'expand', text: t('Expand →') });
    // "Expand" inside the pane has nowhere to expand INTO — screen A is
    // 440 px wide there — so it opens the full /explore in a new tab, on the
    // same thread (`?report=`) and the same record.
    expand.addEventListener('click', () => {
      if (EMBED) {
        const q = EMBED_REPORT ? '?report=' + encodeURIComponent(EMBED_REPORT) : '';
        window.open('/explore' + q + '#q=' + rec.n, '_blank', 'noopener');
        return;
      }
      location.hash = '#q=' + rec.n;
    });
    acts.appendChild(expand);

    if (rec.intent && rec.intent.kpi) {
      const why = el('button', { type: 'button', class: 'btn xs ghost ex-bub-act',
        'data-act': 'why', text: t('Why?') });
      why.addEventListener('click', () => askWhy(rec));
      acts.appendChild(why);

      const cmpBtn = el('button', { type: 'button', class: 'btn xs ghost ex-bub-act',
        'data-act': 'compare', text: t('Compare') });
      cmpBtn.addEventListener('click', () => askCompare(rec));
      acts.appendChild(cmpBtn);
    }

    const pin = el('button', { type: 'button', class: 'btn xs ghost ex-bub-act',
      'data-act': 'pin', text: t('Pin') });
    pin.addEventListener('click', () => pinRecord(rec, pin));
    acts.appendChild(pin);

    ft.appendChild(acts);
    ft.appendChild(el('span', { class: 'ex-bub-meta mono', text: meta.join(' · ') }));
    return card;
  }

  // ═════════════════════════════════════════════════════════════════
  // Views
  // ═════════════════════════════════════════════════════════════════
  function setStatus(text) { if (statusEl) statusEl.textContent = text || ''; }

  async function renderThreads() {
    if (!threadsEl) return;
    threadsEl.textContent = '';
    let threads = [];
    try { threads = await store.threads(); } catch (e) { threads = []; }
    if (!threads.length) {
      threadsEl.appendChild(el('div', { class: 'ex-chat-empty mono',
        text: t('No questions yet. Ask one below.') }));
      return;
    }
    threads.forEach(tv => {
      const row = el('button', { type: 'button', class: 'ex-chat-thread-row',
        'data-report': tv.report_id });
      if (String(tv.report_id) === String(openReport)) row.classList.add('active');
      row.appendChild(el('span', { class: 'ex-chat-thread-title', text: reportTitle(tv.report_id) }));
      // The FIRST question, not the last: every conversation of one report
      // carries the same title, and three rows repeating the newest question
      // of three different conversations named none of them.
      row.appendChild(el('span', { class: 'ex-chat-thread-last',
        text: String(((tv.first || tv.last) || {}).question || '') }));
      row.appendChild(el('span', { class: 'ex-chat-thread-time mono', text: fmtTime(tv.last.ts) }));
      row.addEventListener('click', () => { openThread(tv.report_id); });
      threadsEl.appendChild(row);
    });
  }

  // ── the empty state ───────────────────────────────────────────────────
  // The suggestions are the report's OWN question forms (the same strings
  // the form matcher answers from), so a suggestion that is clicked is
  // guaranteed to be a question this report can answer without a model.
  function introSuggestions(rid) {
    const gen = genOf(rid);
    const out = [];
    ((gen && gen.kpis) || []).forEach(k => {
      if (out.length >= 3 || !k || !k.ready) return;
      const form = (k.question_forms || [])[0];
      out.push(form ? String(form)
                    : t('what was {p1} for the latest period?', { p1: String(k.short || k.name || k.key) }));
    });
    const FALLBACK = [t('what metrics are in the report?'),
                      t('show the total for last week'),
                      t('where did the metric drop the most?')];
    while (out.length < 3) out.push(FALLBACK[out.length]);
    return out.slice(0, 3);
  }

  function renderIntro(rid) {
    const box = el('div', { class: 'ex-chat-intro' });
    box.appendChild(el('h3', { text: t('Ask about the figures in this report') }));
    box.appendChild(el('p', { text: t('Your browser calculates this from the report\'s index. The model is used only if the question didn\'t match any metric.') }));
    const sugg = el('div', { class: 'ex-chat-sugg' });
    introSuggestions(rid).forEach(q => {
      const b = el('button', { type: 'button', 'data-chat-act': 'suggest' });
      b.appendChild(el('span', { text: q }));
      b.appendChild(el('span', { class: 'k', text: t('ask') }));
      b.addEventListener('click', () => { if (inputEl) inputEl.value = q; sendQuestion(q); });
      sugg.appendChild(b);
    });
    box.appendChild(sugg);
    box.appendChild(el('div', { class: 'ex-chat-covline', text: covlineText(coverageText()) }));
    return box;
  }

  // ── P3b: the «⋯» menu, for real ───────────────────────────────────────
  // Switching to another thread (a report from the side list, a conversation
  // from the history) is ONE function: which conversation of that report is
  // open, which record screen A would show, and the fact that the next
  // question inherits nothing from the thread that was open before.
  function openThread(rid) {
    const target = String(rid);
    const base = baseOf(target);
    const conv = convOf(target);
    if (conv) convByBase.set(base, conv); else convByBase.delete(base);
    openReport = target;
    openN = null;
    if (OP.state) OP.state.openAnswer = null;
    histOpen = false;
    renderThreads();
    renderThread();
  }

  // "New conversation": nothing is deleted — the open thread stays where it is
  // and a second one of the SAME report becomes the open one. The next
  // question therefore carries no previous answer as context, which is what
  // the menu item promises.
  async function newConversation() {
    const cur = currentReport();
    let recs = [];
    try { recs = await store.list(cur); } catch (e) { recs = []; }
    if (!recs.length) {
      setStatus(t('This is already a new conversation — it doesn\'t have any questions yet.'));
      return;
    }
    convByBase.set(baseReport(), 'c' + Date.now());
    openReport = currentReport();
    openN = null;
    if (OP.state) OP.state.openAnswer = null;
    histOpen = false;
    await renderThreads();
    await renderThread();
    setStatus(t('New conversation. The previous one is saved — it\'s in "Conversation history".'));
  }

  // "Conversation history": the conversations of THIS report, newest first,
  // each one named by the question it started with. Reading them costs no
  // request — they are rows of this browser's own store.
  let histOpen = false;
  function showHistory(on) {
    histOpen = !!on;
    renderThread();
  }

  async function renderHistory() {
    threadEl.textContent = '';
    const base = baseReport();
    let threads = [];
    try { threads = await store.threads(); } catch (e) { threads = []; }
    const mine = threads.filter(tv => baseOf(tv.report_id) === base);
    const box = el('div', { class: 'ex-chat-hist' });
    box.appendChild(el('h3', { class: 'ex-chat-hist-t', text: t('Conversations for this report') }));
    const back = el('button', { type: 'button', class: 'ex-bub-lnk',
      'data-chat-act': 'hist-back', text: t('← Back to conversation') });
    back.addEventListener('click', () => showHistory(false));
    box.appendChild(back);
    if (!mine.length) {
      box.appendChild(el('div', { class: 'ex-chat-empty mono', text: t('No conversations yet.') }));
    }
    const open = String(currentReport());
    mine.forEach(tv => {
      const start = tv.first || tv.last;
      const row = el('button', { type: 'button', class: 'ex-chat-hist-row',
        'data-conv': String(tv.report_id) });
      if (String(tv.report_id) === open) row.classList.add('active');
      row.appendChild(el('span', { class: 'ex-chat-hist-q', text: String(start.question || '') }));
      row.appendChild(el('span', { class: 'ex-chat-hist-time mono', text: fmtTime(start.ts) }));
      row.appendChild(el('span', { class: 'ex-chat-hist-n mono',
        text: tn(tv.count, '{n} question|{n} questions') }));
      row.addEventListener('click', () => openThread(tv.report_id));
      box.appendChild(row);
    });
    threadEl.appendChild(box);
  }

  // "Copy conversation": the questions and the numbers as text. The SQL
  // never goes — see `transcriptOf`. `navigator.clipboard` where there is
  // one, the old textarea trick where there is not (an http:// stand has no
  // clipboard API at all).
  // The textarea holds the WHOLE conversation for the length of one call. A
  // browser that refuses `copy` (Safari outside a user gesture, a sandboxed
  // frame) threw out of the middle of this — and left that textarea in the
  // page. It is removed in a `finally`, so the only way out is without it.
  function execCopy(text) {
    let ta = null;
    try {
      ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      if (typeof ta.select === 'function') ta.select();
      return !!((typeof document.execCommand === 'function') ? document.execCommand('copy') : false);
    } catch (e) {
      return false;
    } finally {
      try { if (ta && typeof ta.remove === 'function') ta.remove(); } catch (e) { /* nothing left to do */ }
    }
  }
  async function copyThread() {
    let recs = [];
    try { recs = await store.list(currentReport()); } catch (e) { recs = []; }
    if (!recs.length) { setStatus(t('Nothing to copy — the conversation doesn\'t have any answers yet.')); return; }
    const text = transcriptOf(recs, (v, kpi) => valOf(v, kpi));
    let ok = false;
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (e) { ok = false; }
    if (!ok) ok = execCopy(text);
    setStatus(ok ? tn(recs.length, '{n} question with its answer copied.|{n} questions with their answers copied.')
                 : t('Could not copy the conversation — copy the text manually.'));
  }

  // Two renders can overlap (the first paint and the re-render after the
  // index arrives): each one used to clear the thread and, after its own
  // await, append — the reader saw the empty state twice (stand 2026-09-07).
  // The last caller wins; earlier ones stop at the await.
  let renderSeq = 0;
  async function renderThread() {
    if (!threadEl) return;
    if (histOpen) return renderHistory();
    const rid = openReport || currentReport();
    openReport = rid;
    const seq = ++renderSeq;
    let recs = [];
    try { recs = await store.list(rid); } catch (e) { recs = []; }
    if (seq !== renderSeq) return;
    threadEl.textContent = '';
    const byN = new Map();
    recs.forEach(r => byN.set(r.n, r));
    recs.forEach(r => threadEl.appendChild(renderBubble(r, byN)));
    // An empty thread is the one screen that has to TEACH: three questions
    // this report can actually answer, in its own vocabulary.
    if (!recs.length) threadEl.appendChild(renderIntro(rid));
    syncCoverage();
    if (chatCount) chatCount.textContent = String(recs.length);
    postBaseCount(rid);
    const todayEl = document.getElementById('ex-chat-today');
    if (todayEl) {
      const d0 = new Date(); d0.setHours(0, 0, 0, 0);
      const n = recs.filter(r => r.ts >= d0.getTime()).length;
      todayEl.textContent = n ? tn(n, '{n} question today|{n} questions today') : '';
    }
    const titleEl = document.getElementById('ex-chat-title');
    if (titleEl && rid) titleEl.textContent = reportTitle(rid) || t('Report conversation');
    try { threadEl.scrollTop = threadEl.scrollHeight; } catch (e) { /* stub DOM */ }
  }
  function plural(n, f) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return f[0];
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return f[1];
    return f[2];
  }

  // ── Folder state in the chat head ───────────────────────────────
  // Mirrors explore.js's folder controls (#ex-connect-btn / #ex-reconnect-btn
  // / #ex-index-btn live on screen A, hidden while the chat is open): one
  // pill says where the folder stands, one button does the next step.
  const folderEl = document.getElementById('ex-chat-folder');
  const folderBtn = document.getElementById('ex-chat-folder-btn');
  function folderStep() {
    const st = OP.state || {};
    const reconnect = document.getElementById('ex-reconnect-btn');
    const index = document.getElementById('ex-index-btn');
    // Phase 9.4: indexed straight from the report's server index — there is
    // no folder to connect and no button to press, so offering one (or, with
    // a stale handle around, "Allow access") would be a lie.
    if (st.source === 'server' && st.indexed) {
      if (st.serverStale) {
        const load = document.querySelector('.ex-report-card.active .ex-report-load') || document.querySelector('.ex-report-load');
        return { text: t('report recalculated — the data on screen is unchanged'), cls: 'is-warn',
                 btn: load ? t('Refresh index') : null, target: load };
      }
      const parts = st.serverParts || (st.lastFiles || []).length;
      return { text: t('index from server · {p1} {p2}', {p1: parts, p2: plural(parts, [t('partition'), t('partitions'), t('partitions {ctx:2}')])}),
               cls: 'is-ok', btn: null, target: null };
    }
    // Building (download or folder index) — this beats every other branch:
    // nothing is to be pressed while it runs, and the pill shows progress.
    if (st.indexing) {
      const pm = document.getElementById('ex-progress-msg');
      const msg = (pm && pm.textContent) ? pm.textContent : (st.dirHandle ? t('indexing "{p1}"…', {p1: st.dirHandle.name}) : t('loading the report index…'));
      return { text: msg, cls: 'is-warn', btn: null, target: null };
    }
    // A server index that was loaded and then dropped (timeout / "Stop"):
    // the card's own "Load index" is the way back, no folder needed.
    if (st.source === 'server' && !st.indexed && !st.indexing) {
      const load = document.querySelector('.ex-report-card.active .ex-report-load') || document.querySelector('.ex-report-load');
      if (load) return { text: t('index reset{p1}', {p1: (st.lastKill ? '' : '')}), cls: 'is-warn', btn: t('Load index again'), target: load };
    }
    if (reconnect && !reconnect.hidden) return { text: t('folder awaiting permission'), cls: 'is-warn', btn: t('Allow access'), target: reconnect };
    if (!st.dirHandle) {
      // No folder here, but the report has an index on the server: that is
      // the button to offer — a folder is for people who RUN the report.
      const gen = st.reportGen != null ? String(st.reportGen) : null;
      const man = gen && st.serverManifests ? st.serverManifests[gen] : null;
      const load = document.querySelector('.ex-report-card.active .ex-report-load') || document.querySelector('.ex-report-load');
      if (man && load) {
        const n = (man.files || []).length;
        return { text: t('index on server · {p1} {p2}', {p1: n, p2: plural(n, [t('partition'), t('partitions'), t('partitions {ctx:2}')])}), cls: 'is-warn', btn: t('Load index'), target: load };
      }
      // The report's index covers ALL its folders; a folder picker is never
      // the question here. No index yet = the report has not been run since
      // this went live.
      return { text: t('the report index hasn\'t been published yet — recalculate the report (Run)'), cls: 'is-warn', btn: null, target: null };
    }
    const name = st.dirHandle.name || '';
    if (st.indexed) {
      const files = (st.lastFiles || []).length;
      return { text: t('folder "{p1}"{p2}', {p1: name, p2: (files ? ' · ' + files + ' ' + plural(files, [t('file'), t('files'), t('files {ctx:2}')]) : '')}), cls: 'is-ok', btn: null, target: null };
    }
    // Not indexing and not indexed with a folder at hand: either never
    // indexed, or the engine was dropped (timeout / "Stop") — say which.
    const lost = st.lastKill ? t('index reset') : t('not indexed');
    return { text: t('folder "{p1}" — {p2}', {p1: name, p2: lost}), cls: 'is-warn',
             btn: st.lastKill ? t('Refresh index') : t('Refresh index from folder'), target: index };
  }
  function syncFolderHead() {
    syncCoverage();
    if (!folderEl) return;
    const step = folderStep();
    folderEl.textContent = step.text;
    folderEl.className = 'ex-chat-folder mono' + (step.cls ? ' ' + step.cls : '');
    if (folderBtn) {
      folderBtn.hidden = !step.btn;
      if (step.btn) folderBtn.textContent = step.btn;
      folderBtn.onclick = () => { if (step.target) step.target.click(); setTimeout(syncFolderHead, 50); };
    }
  }
  let folderTimer = null;

  function renderChips() {
    if (!chipsEl || !OP.metricChips) return;
    OP.metricChips(chipsEl, (entry) => {
      if (!inputEl) return;
      inputEl.value = entry.kpi.short || entry.kpi.name || entry.kpi.key;
      inputEl.focus();
    });
  }

  function showChat(on) {
    chatSection.hidden = !on;
    SCREEN_A.forEach(s => { s.hidden = !!on; });
    if (navEl) navEl.hidden = true;
    if (on) { renderThreads(); renderThread(); renderChips(); syncFolderHead(); syncCoverage(); }
    clearInterval(folderTimer);
    // The folder pill follows explore.js's own state (connect → permission
    // → index) without that file knowing the chat exists.
    if (on) {
      folderTimer = setInterval(syncFolderHead, 700);
      if (folderTimer && typeof folderTimer.unref === 'function') folderTimer.unref(); // node harness
    }
  }

  chatBtn && chatBtn.addEventListener('click', () => showChat(chatSection.hidden));
  // A clarify from the engine: a transient bubble with chips INSTEAD of a
  // number (never stored — the resolved answer is what history keeps).
  OP.on('clarify', (ev) => {
    // The clarify option's note belonged to the answer that never came.
    clarifyNote = null;
    const prev = threadEl && threadEl.querySelector('.ex-bub-clarify');
    if (prev) prev.remove();
    if (!ev || !threadEl) return;
    const bub = el('div', { class: 'ex-bub ex-bub-a ex-bub-clarify' });
    (ev.clarify || []).forEach(c => {
      bub.appendChild(el('div', { class: 'ex-bub-line',
        text: t('Clarify: "{p1}" is…', {p1: c.word}) }));
      const row = el('div', { class: 'ex-bub-clarify-row' });
      (c.options || []).forEach(o => {
        const b = el('button', { type: 'button', class: 'ex-clarify-chip',
          text: o.dim + ' = ' + o.value });
        b.addEventListener('click', () => ev.resolve(c.word, o.dim, o.value));
        row.appendChild(b);
      });
      const skip = el('button', { type: 'button', class: 'ex-clarify-chip ex-clarify-skip',
        text: t('show without it') });
      skip.addEventListener('click', () => ev.resolve(c.word, null, null));
      row.appendChild(skip);
      bub.appendChild(row);
    });
    threadEl.appendChild(bub);
    threadEl.scrollTop = threadEl.scrollHeight;
  });

  // The controller's doubt (ADR-15) is a NOTE on the answer, never a gate:
  // the held-question card used to swallow the next typed line as its reply
  // («2026» ran the held intent, a real question vanished — stand
  // 2026-09-06). Now the answer always comes, and the doubt travels with it.
  let verifyNote = null;
  OP.on('verify', (ev) => {
    if (!ev) return;
    verifyNote = [t('Checker:'), ev.issue, ev.question].filter(Boolean).join(' ');
    if (typeof ev.proceed === 'function') ev.proceed();
  });

  OP.on('metrics', () => { if (!chatSection.hidden) renderChips(); });

  // ═════════════════════════════════════════════════════════════════
  // Asking from the chat
  // ═════════════════════════════════════════════════════════════════
  // Every form / name of every ready KPI — the vocabulary the follow-up
  // matcher checks a question against before deciding it names no metric.
  function allForms() {
    const out = [];
    (OP.readySemantics() || []).forEach(g => (g.kpis || []).filter(k => k.ready).forEach(k => {
      (k.question_forms || []).forEach(f => out.push(f));
      if (k.short) out.push(k.short);
      if (k.name) out.push(k.name);
    }));
    return out;
  }

  function openIntent() {
    const oa = (OP.state || {}).openAnswer;
    return (oa && oa.intent && oa.intent.kpi) ? oa.intent : null;
  }

  // The dims + synonyms the matcher may cut by: the OPEN answer's own KPI,
  // straight out of the semantics snapshot. `dim_synonyms` is optional — a
  // bundle that declares none simply matches on the dim names.
  function dimContext(intent) {
    const gen = genOf(currentReport()) || (OP.readySemantics() || [])[0];
    const kpi = gen ? OP.kpiOf(gen, intent.kpi) : {};
    return { dims: kpi.dims || [], synonyms: kpi.dim_synonyms || {}, forms: allForms(), gen: gen };
  }

  // Invariant (stand 2026-09-06, three questions "simply vanished"): a typed
  // line is IN the thread before anything else happens, and every outcome
  // — answer, refusal, error — ends as a record. A status line is never the
  // only trace of a question.
  // ── the pending card (P3a) ────────────────────────────────────────────
  // Three stages, and each one is switched by a REAL event explore.js emits
  // (`stage`: intent -> sql -> exec), never by a timer: a card that advances
  // on its own claims the machine got somewhere when it did not. The clock
  // runs at a tenth of a second because 0,4 s and 4 s are different products
  // and a one-second clock cannot tell them apart.
  const PEND_STAGES = ['intent', 'sql', 'exec'];
  function stageText(id) {
    if (id === 'intent') return t('understanding the question');
    if (id === 'sql') return t('building the query');
    return t('calculating from the index');
  }
  let pend = null;                       // the ONE open pending card
  // One question at a time. The send button is disabled while a question is
  // in flight, but Enter, a follow-up chip, "Ask again", an intro
  // suggestion and "Regenerate" all reach sendQuestion() without it —
  // and a second question overwrote `pend`, leaving the first card frozen on
  // screen forever.
  let inFlight = false;

  function pendingCard(q) {
    if (!threadEl) return null;
    const card = el('div', { class: 'ex-bub ex-bub-pending' });
    card.appendChild(el('div', { class: 'ex-bub-q', text: q }));
    const box = el('div', { class: 'ex-pend' });
    const stagesEl = el('div', { class: 'ex-pend-stages' });
    const nodes = {};
    PEND_STAGES.forEach(id => {
      nodes[id] = el('span', { class: 'ex-pend-stage', 'data-stage': id, text: stageText(id) });
      stagesEl.appendChild(nodes[id]);
    });
    box.appendChild(stagesEl);
    const row = el('div', { class: 'ex-pend-row' });
    const timerEl = el('span', { class: 'ex-pend-timer' });
    const cancelEl = el('button', { type: 'button', class: 'ex-pend-cancel',
      'data-chat-act': 'cancel', text: t('Cancel {ctx:2}') });
    row.appendChild(timerEl);
    row.appendChild(cancelEl);
    box.appendChild(row);
    card.appendChild(box);

    const t0 = Date.now();
    const showTime = () => {
      const secs = (Date.now() - t0) / 1000;
      timerEl.textContent = t('{p1} s', { p1: OP_I18N.fmtNum(secs,
        { minimumFractionDigits: 1, maximumFractionDigits: 1 }) });
    };
    showTime();
    const timer = setInterval(showTime, 100);
    if (timer && typeof timer.unref === 'function') timer.unref();   // node harness

    const ctl = {
      el: card, question: q, cancelled: false, finished: false,
      stage(id) {
        let past = true;
        PEND_STAGES.forEach(sid => {
          const n = nodes[sid];
          if (!n || !n.classList) return;
          if (sid === id) { past = false; n.classList.add('is-on'); n.classList.remove('is-done'); return; }
          n.classList.remove('is-on');
          n.classList.toggle('is-done', past);
        });
      },
      stop() { clearInterval(timer); },
      // The answer landed: the clock stops and "Cancel" goes dead before
      // the record is written, so a late click cannot cancel the NEXT
      // question. The card itself is replaced by renderThread().
      finish() {
        if (this.finished) return;
        this.finished = true; this.stop();
        try { cancelEl.disabled = true; } catch (e) { /* stub DOM */ }
      },
      remove() { this.stop(); try { card.remove(); } catch (e) { /* stub DOM */ } if (pend === this) pend = null; },
    };
    cancelEl.addEventListener('click', () => cancelPending(ctl, cancelEl));
    ctl.stage('intent');
    threadEl.appendChild(card);
    threadEl.scrollTop = threadEl.scrollHeight;
    return ctl;
  }

  // "Cancel" is a real abort: explore.js aborts the request that is in
  // flight (its own AbortController) and the two gates in runQuery drop the
  // answer that was already on its way. The card does NOT vanish — an
  // operator who cancelled must still see WHICH question they cancelled.
  function cancelPending(ctl, btn, fromPage) {
    // `finished` = the answer already arrived; there is nothing to cancel.
    if (!ctl || ctl.cancelled || ctl.finished) return;
    ctl.cancelled = true;
    // Whatever the page does with the request it was running, the operator is
    // free to ask the next question right away.
    inFlight = false;
    if (btn) btn.disabled = true;
    ctl.stop();
    // `fromPage` = explore.js is TELLING us it cancelled (its own 'cancelled'
    // event). Calling cancelAsk() back would raise the page's sticky flag
    // again — and the next question, not this one, would be the one that dies.
    if (!fromPage) {
      try { if (OP.cancelAsk) OP.cancelAsk(); } catch (e) { /* the card still has to close */ }
    }
    ctl.el.textContent = '';
    ctl.el.className = 'ex-bub ex-bub-cancelled';
    ctl.el.appendChild(el('div', { class: 'ex-bub-q', text: ctl.question }));
    const box = el('div', { class: 'ex-bub-ask' });
    box.appendChild(el('div', { class: 'ex-bub-ask-t', text: t('Cancelled — the answer was not fully calculated.') }));
    const again = el('button', { type: 'button', class: 'ex-bub-opt', 'data-chat-act': 'again' });
    again.appendChild(el('span', { text: t('Ask again') }));
    again.appendChild(el('span', { class: 'k', text: t('the same question again') }));
    again.addEventListener('click', () => sendQuestion(ctl.question));
    box.appendChild(again);
    ctl.el.appendChild(box);
    setStatus(t('Cancelled.'));
  }

  // The stages the page really passed, and the page's own "cancelled" (the
  // engine was already running when the button was pressed).
  OP.on('stage', (ev) => {
    if (pend && !pend.cancelled && ev && ev.stage) pend.stage(String(ev.stage));
  });
  OP.on('cancelled', () => { if (pend && !pend.cancelled) cancelPending(pend, null, true); });

  function showPending(q) {
    pend = pendingCard(q);
    return pend;
  }
  async function recordFailure(q, why) {
    try {
      await store.add(currentReport(), { question: q, kind: 'error', source: 'chat', notes: [why || t('Couldn\'t answer the question.')] });
    } catch (e) { /* history is a convenience */ }
    setCount();
    if (!chatSection.hidden) renderThread();
  }

  // ── idempotency (P3b) ─────────────────────────────────────────────────
  // The same question asked twice while the first is still in flight is ONE
  // request: the second caller gets the FIRST promise and waits for its
  // answer. `inFlight` still keeps a DIFFERENT question from starting on top
  // of one — the pending card is a single card.
  const inflight = new Map();            // askKey -> the promise in flight

  // `qArg` — a question that came from a button (a follow-up chip, "Ask
  // again", "Regenerate", "Clarify"); with none, the composer's text.
  // `opts`: {parentN, parentIntent} — the card this question hangs off, so a
  // clarification of the THIRD answer is a child of the third, not of the
  // last; {fixedKpi} — the metric is settled by the reader; {regen}.
  function sendQuestion(qArg, opts) {
    const q = String(qArg != null ? qArg : ((inputEl && inputEl.value) || '')).trim();
    if (!q) return Promise.resolve(false);
    const key = askKey(q, Object.assign({}, opts, { report: currentReport() }));
    const already = inflight.get(key);
    if (already) return already;         // the very same question — one request
    if (inFlight) return Promise.resolve(false);
    inFlight = true;
    // A new question starts a new dedup window: two 'answer' events belong to
    // ONE question, and the question before this one is settled.
    answerSeen.clear();
    histOpen = false;
    // What goes INTO the map is what the caller gets — the wrapper, not the
    // bare run: the first caller was handed `p.then(…)` and every later one
    // the raw `p`, so «the same question is one request» was true of the
    // request and false of the promise.
    const p = runQuestion(q, opts || {}, qArg).then(
      (v) => { inflight.delete(key); return v; },
      (e) => { inflight.delete(key); throw e; });
    inflight.set(key, p);
    return p;
  }

  // The metric "Clarify" pinned for the question in flight. The 'answer'
  // event is the page's report of the ROUTING, and the pin is the chat's own
  // option — so the chat is the one that puts it in the record, and
  // "Regenerate" has it to pass back.
  let pendingFixedKpi = null;

  async function runQuestion(q, opts, qArg) {
    pendingFixedKpi = (opts && opts.fixedKpi) || null;
    const pending = showPending(q);
    // No index = nothing to answer over. Say which step is missing instead
    // of letting the question fall to the raw-catalogue route (a server
    // model call that this deployment may not even have).
    // An index that is being built is not a refusal: the question waits
    // for it and goes by itself.
    if (OP.state && !OP.state.indexed && OP.state.indexing && OP.whenIndexed) {
      setStatus(t('The index is building — the question will be sent automatically once it\'s ready.'));
      if (sendEl) sendEl.disabled = true;
      const ok = await OP.whenIndexed();
      if (sendEl) sendEl.disabled = false;
      if (!ok) {
        const w = t('The index didn\'t build — {p1}.', {p1: folderStep().text});
        setStatus(w);
        // The card (and its 100 ms clock) goes with the question: the record
        // recordFailure() writes is what stands in the thread instead.
        if (pending) pending.remove();
        inFlight = false;
        await recordFailure(q, w);
        return;
      }
      setStatus('');
    }
    if (!(OP.state && OP.state.indexed)) {
      const step = folderStep();
      const why = OP.state && OP.state.lastKill ? ' (' + OP.state.lastKill + ')' : '';
      const w = step.btn ? t('First: "{p1}" — the button at the top of the chat{p2}.', {p1: step.btn, p2: why})
                         : (OP.state && OP.state.indexing ? t('The index is still building — wait a moment.') : step.text.charAt(0).toUpperCase() + step.text.slice(1) + why + '.');
      setStatus(w);
      syncFolderHead();
      if (pending) pending.remove();
      inFlight = false;
      await recordFailure(q, w);
      return;
    }
    if (sendEl) sendEl.disabled = true;
    setStatus(t('Calculating…'));
    try {
      // "Clarify" names the card it came from: the intent of THAT answer is
      // the context, and its `n` is the parent — a clarification of the third
      // answer must not become a child of the newest one.
      const fromCard = opts.parentIntent && opts.parentIntent.kpi ? opts.parentIntent : null;
      const prev = fromCard || openIntent();
      const parentN = opts.parentN != null ? Number(opts.parentN) : openN;
      const ctx = prev ? dimContext(prev) : null;
      // A metric the reader pinned is not a follow-up to settle by cut and
      // period: the question goes through the routing WITH that metric fixed.
      const hit = (prev && !opts.fixedKpi) ? matchFollowUp(q, prev, ctx) : null;
      if (hit && ctx.gen) {
        // The deterministic case: no server round trip, no model. The intent
        // is the OPEN one with a new cut/period, and it runs through the very
        // same finishIntent every "Ask" answer goes through.
        const intent = Object.assign({}, prev, { dims: hit.dims, filters: hit.filters });
        OP.state.askQuestion = q;
        OP.state.askSource = 'followup';
        OP.state.askParentN = parentN;
        let landedFu = false;
        const offFu = ['answer', 'clarify'].map(ev => OP.on(ev, () => { landedFu = true; }));
        let okFu = false;
        try { okFu = await OP.finishIntent(OP.withGen(intent, ctx.gen), ctx.gen,
          t('clarification of the previous answer ({p1})', {p1: hit.matched.join(', ')})); }
        finally { offFu.forEach(off => { if (typeof off === 'function') off(); }); }
        if (!okFu || !landedFu) {
          if (pending && pending.cancelled) return;
          const sq = document.getElementById('ex-sql-status');
          await recordFailure(q, ((sq && sq.textContent) || '').trim() || t('The follow-up could not be computed.'));
          return;
        }
      } else {
        // Everything else keeps the normal routing — and carries the OPEN
        // answer's intent so the server can name the previous metric in the
        // prompt. The thread itself is never sent.
        OP.state.askSource = null;   // the page's askVia (form/model) names the source
        OP.state.askParentN = parentN;
        // `ask()` answers true for "handled" — and a provider error, a
        // rejected key or a missing model are all "handled": the message
        // lands in explore's own status line, which the chat does not show.
        // The thread only gets a record from an 'answer' (or a 'clarify' /
        // 'verify' card), so anything that returned true WITHOUT one of those
        // is a question that went nowhere: keep the text in the box and say
        // why, instead of clearing it as if it had been answered.
        let landed = false;
        const offs = ['answer', 'clarify', 'verify'].map(ev => OP.on(ev, () => { landed = true; }));
        let ok = false;
        try { ok = await OP.ask(q, { prevIntent: prev, fixedKpi: opts.fixedKpi || null }); }
        finally { offs.forEach(off => { if (typeof off === 'function') off(); }); }
        if (!ok || !landed) {
          // A question the operator cancelled is not a question that failed:
          // the "Cancelled" card is already its trace.
          if (pending && pending.cancelled) return;
          // Two places explore.js reports to: its question line (provider /
          // key / validation errors) and the SQL row's line (a model-written
          // query DuckDB refused). Either is the reason the chat has to show.
          const nl = document.getElementById('ex-nl-status');
          const sq = document.getElementById('ex-sql-status');
          const busy = ['Calculating…', 'Asking…', 'Running…'].map(k => String(OP_I18N.t(k)).replace(/…$/, '').split(' ')[0]).filter(Boolean);  // progress lines, any language, are no reason
          const why = [(nl && nl.textContent) || '', (sq && sq.textContent) || ''].map(tv => tv.trim()).filter(tv => tv && !busy.some(w => tv.indexOf(w) === 0)).join(' · ');
          setStatus(why || t('Couldn\'t answer the question.'));
          // The umbrella: whatever the stage-level rows said, a question that
          // produced no answer is ONE more row an admin can count.
          if (OP.reportClientError) OP.reportClientError('chat_no_answer', why || 'no answer', { question: q });
          await recordFailure(q, why || t('Couldn\'t answer the question.'));
          return;
        }
      }
      if (inputEl && (qArg == null || String(inputEl.value || '').trim() === q)) {
        inputEl.value = ''; autoGrow();
      }
      if (!(pending && pending.cancelled)) setStatus('');
    } catch (e) {
      if (pending && pending.cancelled) return;
      setStatus(t('Couldn\'t answer the question.'));
      await recordFailure(q, t('Could not answer the question: {p1}', {p1: String(e && e.message || e).slice(0, 200)}));
    } finally {
      inFlight = false;
      pendingFixedKpi = null;      // never inherited by the next answer
      if (sendEl) sendEl.disabled = false;
      // `pending` lives until renderThread() rebuilds the thread from records
      // (answer or failure) — a question is never on screen twice.
      // A CANCELLED card stays until the next renderThread() rebuilds the
      // thread from the records; it is not stored, so it does not survive
      // that — but blank space in its place is what P3a forbids, and until
      // then it is what the operator has.
      if (pending && !pending.cancelled && threadEl.querySelector('.ex-bub[data-n]')) pending.remove();
    }
  }

  // Phase 4.5: the value lookup behind "why is the number in the report 25.5?" searches
  // the answers this browser already holds BEFORE it re-runs any metric. The
  // thread is this file's data, so it is lent to explore.js rather than
  // teaching explore.js the store. Records never leave the browser here
  // either — the lookup is pure client-side arithmetic over `rec.rows`.
  OP.setHistoryProvider && OP.setHistoryProvider(async () => {
    const rid = openReport || currentReport();
    if (rid == null) return [];
    try { return await store.list(rid); } catch (e) { return []; }
  });

  // Not `addEventListener('click', sendQuestion)`: the click EVENT would
  // arrive as the question argument.
  sendEl && sendEl.addEventListener('click', () => sendQuestion());
  inputEl && inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
  });
  // Grow with the text (1–5 lines), never a resize handle.
  function autoGrow() {
    if (!inputEl || !inputEl.style) return;
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(120, inputEl.scrollHeight || 0) + 'px';
  }
  inputEl && inputEl.addEventListener('input', autoGrow);

  // "Clear chat": this report's thread, this browser only (the rows
  // never left it). Confirm first — there is no undo.
  const clearEl = document.getElementById('ex-chat-clear');
  clearEl && clearEl.addEventListener('click', async () => {
    const rid = openReport || currentReport();
    let recs = [];
    try { recs = await store.list(rid); } catch (e) { recs = []; }
    if (!recs.length) { setStatus(t('Chat is empty.')); return; }
    if (!confirm(t('Delete {p1} {p2} from the chat "{p3}"? This can\'t be undone.', {p1: recs.length, p2: plural(recs.length, [t('question'), t('questions'), t('questions {ctx:2}')]), p3: reportTitle(rid)}))) return;
    try { await store.clear(rid); } catch (e) { setStatus(t('Couldn\'t clear the chat.')); return; }
    openN = null;
    if (OP.state) OP.state.openAnswer = null;
    await renderThreads();
    await renderThread();
    setStatus(t('Chat cleared.'));
  });

  // "Why?"/"Compare" need the index just like a typed question does:
  // wait for one that is building, name the missing step otherwise — the
  // old "This answer's metric is no longer ready" explained nothing.
  async function genReady(rec) {
    let gen = genOf(rec.report_id);
    if (gen) return gen;
    if (OP.state && OP.state.indexing && OP.whenIndexed) {
      setStatus(t('The index is building — the action will run once it\'s ready.'));
      if (await OP.whenIndexed()) { setStatus(''); gen = genOf(rec.report_id); if (gen) return gen; }
    }
    const step = folderStep();
    setStatus(step.btn ? t('First: "{p1}" — the button at the top of the chat.', {p1: step.btn})
                       : step.text.charAt(0).toUpperCase() + step.text.slice(1) + '.');
    return null;
  }

  // ── bubble actions ────────────────────────────────────────────────────
  async function askWhy(rec) {
    if (!rec.intent) { setStatus(t('This answer has no metric — "Why?" has nothing to apply to.')); return; }
    const gen = await genReady(rec);
    if (!gen) return;
    OP.state.askQuestion = t('Why: {p1}', {p1: rec.question});
    OP.state.askSource = 'why';
    OP.state.askParentN = rec.n;
    setStatus(t('Working it out…'));
    await OP.whyCard(OP.withGen({ kpi: rec.intent.kpi, dims: [], filters: rec.intent.filters || [] }, gen),
                     gen, { period: rec.intent.filters || [] });
    setStatus('');
  }

  async function askCompare(rec) {
    if (!rec.intent) { setStatus(t('This answer has no metric — there\'s nothing to compare.')); return; }
    const gen = await genReady(rec);
    if (!gen) return;
    const shifted = OP.shiftPeriod(rec.intent.filters || []);
    if (!shifted) { setStatus(t('No period specified — nothing to compare.')); return; }
    OP.state.askQuestion = t('{p1} — previous period', {p1: rec.question});
    OP.state.askSource = 'compare';
    OP.state.askParentN = rec.n;
    setStatus(t('Calculating the previous period…'));
    await OP.finishIntent(OP.withGen(Object.assign({}, rec.intent, { filters: shifted }), gen),
                          gen, t('same dimension, previous period'));
    setStatus('');
  }

  async function pinRecord(rec, btn) {
    if (!rec.sql) { setStatus(t('This answer has no SQL — there\'s nothing to pin.')); return; }
    btn.disabled = true;
    try {
      const r = await fetch('/api/explore/questions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: rec.question.slice(0, 120) || t('Question from chat'),
          question: rec.question, sql: rec.sql, table_ids: [] }),
      });
      setStatus(r.ok ? t('Pinned.') : t('Couldn\'t pin ({p1}).', {p1: r.status}));
    } catch (e) {
      setStatus(t('Couldn\'t pin.'));
    } finally {
      btn.disabled = false;
    }
  }

  // ═════════════════════════════════════════════════════════════════
  // `#q=N` — screen A showing one stored answer
  // ═════════════════════════════════════════════════════════════════
  // The rows come straight out of the record; nothing is re-run and nothing
  // is fetched. "Recalculate" is offered only while the folder is actually
  // indexed — a stored answer over a folder that is no longer connected is
  // history, and pretending it can be refreshed is a lie.
  async function showRecord(n) {
    const rid = openReport || currentReport();
    let rec = null;
    try { rec = await store.get(rid, n); } catch (e) { rec = null; }
    if (!rec) return false;
    showChat(false);
    openReport = rid;
    openN = rec.n;
    // Opening a record IS choosing its report: pin it, and when nothing is
    // indexed yet let the page pick the report's server index up by itself
    // ("Expand" from the pane lands on a fresh tab with no index).
    // The REPORT, never the conversation: `state.reportGen` is matched against
    // generator ids by explore.js itself, and «7#c17» matches none of them.
    if (OP.state && rec.report_id && baseOf(rec.report_id) !== 'catalog') {
      OP.state.reportGen = baseOf(rec.report_id);
      if (OP.renderReports) OP.renderReports();
      if (!OP.state.indexed && !OP.state.indexing && OP.maybeServerAutoIndex) OP.maybeServerAutoIndex();
    }
    OP.setAnswerTitle(rec.question);
    OP.setSqlBox(rec.sql);
    // The value column is formatted in the metric's own terms (percent for a
    // ratio, the unit otherwise) — renderResult reads the KPI from state, and
    // a rehydrated record knows its KPI too (raw 0.9988 for 99,88 % was this).
    if (OP.state) OP.state.lastKpi = rec.kpi || null;
    OP.renderResult({ columns: rec.cols || [], rows: rec.rows || [],
      total: rec.total, ms: null,
      partitions_used: (rec.provenance || {}).partitions_used || [],
      partitions_expected: ((rec.provenance || {}).partitions_used || []).length });
    OP.state.openAnswer = { question: rec.question, intent: rec.intent, kpi: rec.kpi,
      sql: rec.sql, kind: rec.kind };
    if (navEl) {
      navEl.hidden = false;
      if (recalcEl) recalcEl.hidden = !(OP.state && OP.state.indexed && rec.intent);
    }
    const all = await store.list(rid);
    const idx = all.findIndex(r => r.n === rec.n);
    if (prevEl) prevEl.disabled = idx <= 0;
    if (nextEl) nextEl.disabled = idx < 0 || idx >= all.length - 1;
    if (prevEl) prevEl.dataset.n = idx > 0 ? String(all[idx - 1].n) : '';
    if (nextEl) nextEl.dataset.n = (idx >= 0 && idx < all.length - 1) ? String(all[idx + 1].n) : '';
    return true;
  }

  function onHash() {
    const n = parseQHash(location.hash);
    if (n == null) return;
    showRecord(n);
  }
  window.addEventListener('hashchange', onHash);

  backEl && backEl.addEventListener('click', () => {
    try { history.replaceState(null, '', location.pathname + location.search); }
    catch (e) { location.hash = ''; }
    showChat(true);
  });
  prevEl && prevEl.addEventListener('click', () => {
    if (prevEl.dataset.n) location.hash = '#q=' + prevEl.dataset.n;
  });
  nextEl && nextEl.addEventListener('click', () => {
    if (nextEl.dataset.n) location.hash = '#q=' + nextEl.dataset.n;
  });
  recalcEl && recalcEl.addEventListener('click', async () => {
    const rid = openReport || currentReport();
    const rec = await store.get(rid, openN);
    const gen = rec && genOf(rid);
    if (!rec || !rec.intent || !gen) return;
    OP.state.askQuestion = rec.question;
    OP.state.askSource = 'recalc';
    OP.state.askParentN = rec.n;
    await OP.finishIntent(OP.withGen(rec.intent, gen), gen, t('recalculated for current folder'));
  });

  // `?report=` scopes the page to one report's thread — the same choice the
  // report strip makes when a card is clicked, so `state.reportGen` is where
  // it belongs and `currentReport()` needs no special case.
  const paramGen = reportGenOf(EMBED_REPORT);
  if (paramGen != null) {
    if (OP.state) OP.state.reportGen = paramGen;
    openReport = paramGen;
  }

  // First paint: the count is what the header button shows before anything is
  // asked, and a deep link lands on its record rather than an empty screen A.
  setCount();
  onHash();
  // …and the conversation the reader left off in, as soon as the store can
  // say which one it was (one read, no request — see `restoreConversation`).
  restoreConversation().then(() => {
    setCount();
    if (chatSection && !chatSection.hidden) { renderThreads(); renderThread(); }
  }).catch(() => {});

  // ══════════════════════════════════════════════════════════════════
  // TEST-ONLY hook — same discipline (and the same localhost gate) as
  // `__opChatBackend` above: `sendQuestion` returns the promise the
  // idempotency rule is ABOUT, and a click cannot hand it back. Nothing in
  // this file reads it.
  // ══════════════════════════════════════════════════════════════════
  try {
    if (location.hostname === 'localhost' || location.hostname === '127.0.0.1') {
      window.__opChatApi = { sendQuestion: sendQuestion, currentReport: currentReport, renderThread: renderThread, syncCoverage: syncCoverage };
    }
  } catch (e) { /* no window, no hook */ }

  // `mode=chat` opens the chat view immediately — the pane IS the chat.
  if (EMBED_MODE === 'chat') showChat(true);
})();
