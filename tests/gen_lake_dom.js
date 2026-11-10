/* Phase 9.3 — the report index (the “lake”) uploaded by gen-engine.js after a
 * successful publish. Same idiom as tests/gen_engine_observe.js and
 * tests/explore_dom.js: no npm deps, no jsdom — the step is driven with a
 * FAKE worker (`send`), a FAKE fetch and plain sinks for the two UI hooks,
 * because everything it does is a message/endpoint sequence.
 *
 *   node tests/gen_lake_dom.js
 *
 * What is pinned here:
 *   - files are matched to the catalogue by file NAME, one record per
 *     partition (a workbook with two sheets = two partitions);
 *   - a cache HIT is never read from the folder and never re-indexed;
 *   - every part is POSTed before the manifest PUT, and the PUT carries the
 *     nine fields app/api/lake.py's LakeFileEntry requires;
 *   - a 413 on a part aborts the step with ONE warning and NO manifest PUT;
 *   - a generator whose /generator/semantics is a 404 does nothing at all;
 *   - `"lake": false` in the manifest does nothing at all, without any fetch.
 */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub

const assert = require('assert');
const path = require('path');
const { matchFilesToCatalog, lakeStep } =
  require(path.join(__dirname, '..', 'static', 'js', 'gen-engine.js'));

let n = 0;
function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => { n++; console.log('ok - ' + name); })
    .catch((e) => { console.error('FAIL - ' + name); console.error(e); process.exitCode = 1; });
}

// ── fixtures ────────────────────────────────────────────────────────────
// Two workbooks in the run's folder; the catalogue knows three partitions:
// one file backs TWO sheets of one table, the other backs a second table.
// `notes.xlsx` is in the folder but in no catalogue partition.
const FILES = [
  { name: 'sl_2026_01.xlsx', size: 1000, mtime_ms: 111, dir: 0 },
  { name: 'sl_2026_02.xlsx', size: 2000, mtime_ms: 222, dir: 1 },
  { name: 'notes.xlsx', size: 30, mtime_ms: 333, dir: 0 },
];

const TABLES = [
  {
    table_id: 't1', name: 'sl_zakazy', header_row: 3,
    partitions: [
      { key: 'p1', file: 'sl_2026_01.xlsx', sheet: 'Orders' },
      { key: 'p2', file: 'sl_2026_01.xlsx', sheet: 'Cancellations' },
      { key: 'p3', file: 'sl_2026_02.xlsx', sheet: 'Orders' },
    ],
  },
  {
    table_id: 't2', name: 'sl_prichiny', header_row: 0,
    partitions: [{ key: 'p4', file: 'sl_2026_02.xlsx', sheet: 'Reasons' }],
  },
];

const KEYS = ['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb', 'cccccccccccccccc', 'dddddddddddddddd'];

function mkBuf(nBytes) { return new ArrayBuffer(nBytes); }

// A fake worker: records every message and answers per type. `cached` is the
// per-file hit mask the probe reports back.
function mkWorker(opts) {
  opts = opts || {};
  const calls = [];
  const cached = opts.cached || [];
  return {
    calls,
    send(type, payload) {
      calls.push({ type, payload });
      if (type === 'explore:init') return Promise.resolve({ type: 'explore:ready' });
      if (type === 'explore:cache_probe') {
        const files = payload.files || [];
        return Promise.resolve({
          keys: files.map((f, i) => KEYS[i]),
          cached: files.map((f, i) => !!cached[i]),
          parser_version: 'pv-7',
        });
      }
      if (type === 'explore:index') return Promise.resolve({ indexed: 1, errors: [] });
      if (type === 'explore:cache_export') {
        return Promise.resolve({
          parts: (payload.keys || []).map((k, i) => ({ key: k, buf: mkBuf(100 + i) })),
        });
      }
      return Promise.resolve({});
    },
  };
}

// A fake fetch: records every request and answers from a route table.
function mkFetch(routes) {
  const calls = [];
  const f = (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || 'GET', body: opts && opts.body });
    for (const r of routes) {
      if (url.indexOf(r.match) >= 0 && (!r.method || r.method === ((opts && opts.method) || 'GET'))) {
        return Promise.resolve(r.res(url, opts));
      }
    }
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
  };
  f.calls = calls;
  return f;
}

const okJson = (body) => ({ ok: true, status: 200, json: () => Promise.resolve(body) });
const OK = { ok: true, status: 200, json: () => Promise.resolve({ ok: true }) };
const NOT_FOUND = { ok: false, status: 404, json: () => Promise.resolve({}) };

function mkCtx(over) {
  const w = over.worker;
  const st = { status: [], warns: [], read: [] };
  const ctx = Object.assign({
    dashId: 'dash-1',
    runId: 42,
    pyodideBase: '/static/pyodide/',
    lakeEnabled: true,
    files: FILES,
    readFile: (rec) => { st.read.push(rec.name + '|' + (rec.sheet || '')); return Promise.resolve(mkBuf(8)); },
    send: w ? w.send : () => Promise.resolve({}),
    setStatus: (t) => st.status.push(t),
    showWarn: (t) => st.warns.push(t),
  }, over);
  return { ctx, st };
}

const HAPPY_ROUTES = [
  { match: '/generator/semantics', res: () => okJson({ tables: [] }) },
  { match: '/api/explore/catalog', res: () => okJson({ tables: TABLES }) },
  { match: '/lake/parts', method: 'POST', res: () => OK },
  { match: '/lake/manifest', method: 'PUT', res: () => OK },
];

// ── matchFilesToCatalog ─────────────────────────────────────────────────
check('matchFilesToCatalog: one record per catalogue partition, by name+sheet', () => {
  const { matched, unmatched } = matchFilesToCatalog(FILES, TABLES);
  assert.strictEqual(matched.length, 4);
  assert.deepStrictEqual(matched.map(m => m.name + '/' + m.sheet), [
    'sl_2026_01.xlsx/Orders', 'sl_2026_01.xlsx/Cancellations',
    'sl_2026_02.xlsx/Orders', 'sl_2026_02.xlsx/Reasons',
  ]);
  // the table NAME (what the worker registers), the partition key, the
  // header row and the folder index all ride along
  assert.strictEqual(matched[0].table, 'sl_zakazy');
  assert.strictEqual(matched[0].header_row, 3);
  assert.strictEqual(matched[0].partition, 'p1');
  assert.strictEqual(matched[3].table, 'sl_prichiny');
  assert.strictEqual(matched[3].header_row, 0);
  assert.strictEqual(matched[2].dir, 1, 'the file lives in the second folder');
  assert.deepStrictEqual(unmatched, ['notes.xlsx']);
});

check('matchFilesToCatalog: a catalogue file that is not in the folder is skipped', () => {
  const { matched } = matchFilesToCatalog([FILES[2]], TABLES);
  assert.strictEqual(matched.length, 0);
});

// ── the happy path ──────────────────────────────────────────────────────
check('lakeStep: init → probe → index → export → parts → manifest', async () => {
  const w = mkWorker();
  const F = mkFetch(HAPPY_ROUTES);
  const { ctx, st } = mkCtx({ worker: w, fetch: F });
  const res = await lakeStep(ctx);
  assert.deepStrictEqual(res.ok, true);
  assert.strictEqual(res.parts, 4);

  // worker sequence
  assert.deepStrictEqual(w.calls.map(c => c.type), [
    'explore:init', 'explore:cache_probe',
    'explore:index', 'explore:index', 'explore:index', 'explore:index',
    'explore:cache_export',
  ]);
  // the init carries the catalogue AND the pyodide base the worker's
  // /explore side boots from
  assert.strictEqual(w.calls[0].payload.pyodideBase, '/static/pyodide/');
  assert.strictEqual(w.calls[0].payload.catalog.tables.length, 2);
  // the probe asks about exactly the matched partitions, with the fields the
  // key recipe needs — and nothing else
  assert.deepStrictEqual(w.calls[1].payload.files.map(f => f.sheet),
    ['Orders', 'Cancellations', 'Orders', 'Reasons']);
  assert.deepStrictEqual(Object.keys(w.calls[1].payload.files[0]).sort(),
    ['header_row', 'mtime_ms', 'name', 'sheet', 'size'].sort());
  // only the LAST index message closes the run (semantic views + OPFS sync)
  assert.deepStrictEqual(w.calls.slice(2, 6).map(c => c.payload.final),
    [false, false, false, true]);
  // each index message carries one file, with the key the worker minted
  assert.strictEqual(w.calls[2].payload.files.length, 1);
  assert.strictEqual(w.calls[2].payload.files[0].cache_key, KEYS[0]);
  assert.strictEqual(w.calls[2].payload.files[0].table, 'sl_zakazy');
  // the export asks for every key of this run
  assert.deepStrictEqual(w.calls[6].payload.keys, KEYS);

  // endpoint sequence: semantics, catalog, N parts, then the manifest
  assert.deepStrictEqual(F.calls.map(c => c.method + ' ' + c.url.replace(/^.*dashboards/, 'dashboards')), [
    'GET dashboards/dash-1/generator/semantics',
    'GET /api/explore/catalog',
    'POST dashboards/dash-1/lake/parts',
    'POST dashboards/dash-1/lake/parts',
    'POST dashboards/dash-1/lake/parts',
    'POST dashboards/dash-1/lake/parts',
    'PUT dashboards/dash-1/lake/manifest',
  ]);
  // every part upload is a FormData with `key` + `file`
  const fd = F.calls[2].body;
  assert.ok(typeof fd.get === 'function', 'part body is a FormData');
  assert.strictEqual(fd.get('key'), KEYS[0]);
  assert.ok(fd.get('file'), 'part body carries the parquet blob');

  // status line, in Russian, k/N
  assert.ok(st.status.indexOf('Report index: 0/4') >= 0, st.status.join(' | '));
  assert.ok(st.status.indexOf('Report index: 4/4') >= 0, st.status.join(' | '));
  assert.ok(/^Report index loaded \(4 partitions, 0\.0 MB\)$/.test(st.status[st.status.length - 1]),
    st.status[st.status.length - 1]);
  assert.deepStrictEqual(st.warns, []);
});

check('lakeStep: the manifest PUT carries every field LakeFileEntry requires', async () => {
  const w = mkWorker();
  const F = mkFetch(HAPPY_ROUTES);
  const { ctx } = mkCtx({ worker: w, fetch: F });
  await lakeStep(ctx);
  const put = F.calls[F.calls.length - 1];
  const body = JSON.parse(put.body);
  assert.strictEqual(body.run_id, 42);
  assert.strictEqual(body.parser_version, 'pv-7');   // from the probe, not the page
  assert.strictEqual(body.files.length, 4);
  const REQUIRED = ['name', 'size', 'mtime_ms', 'sheet', 'header_row', 'key', 'table', 'partition', 'bytes'];
  body.files.forEach((f, i) => {
    REQUIRED.forEach(k => assert.ok(k in f, 'entry ' + i + ' is missing ' + k));
  });
  assert.deepStrictEqual(body.files[0], {
    name: 'sl_2026_01.xlsx', size: 1000, mtime_ms: 111, sheet: 'Orders',
    header_row: 3, key: KEYS[0], table: 'sl_zakazy', partition: 'p1', bytes: 100,
  });
  assert.strictEqual(body.files[3].table, 'sl_prichiny');
  assert.strictEqual(body.files[3].bytes, 103);
});

// ── cache hits ──────────────────────────────────────────────────────────
check('lakeStep: a probe HIT is neither read from the folder nor re-indexed', async () => {
  const w = mkWorker({ cached: [true, false, true, false] });
  const F = mkFetch(HAPPY_ROUTES);
  const { ctx, st } = mkCtx({ worker: w, fetch: F });
  const res = await lakeStep(ctx);
  assert.strictEqual(res.ok, true);
  const indexed = w.calls.filter(c => c.type === 'explore:index');
  assert.strictEqual(indexed.length, 2, 'only the two misses are indexed');
  assert.deepStrictEqual(indexed.map(c => c.payload.files[0].sheet), ['Cancellations', 'Reasons']);
  assert.deepStrictEqual(indexed.map(c => c.payload.final), [false, true]);
  // the hits' workbooks are never opened
  assert.deepStrictEqual(st.read, ['sl_2026_01.xlsx|Cancellations', 'sl_2026_02.xlsx|Reasons']);
  // hits still count as done, and they are still exported and uploaded
  assert.ok(st.status.indexOf('Report index: 2/4') >= 0, st.status.join(' | '));
  assert.strictEqual(res.parts, 4);
});

check('lakeStep: all hits — no index message at all, still uploads', async () => {
  const w = mkWorker({ cached: [true, true, true, true] });
  const F = mkFetch(HAPPY_ROUTES);
  const { ctx, st } = mkCtx({ worker: w, fetch: F });
  const res = await lakeStep(ctx);
  assert.strictEqual(w.calls.filter(c => c.type === 'explore:index').length, 0);
  assert.deepStrictEqual(st.read, []);
  assert.strictEqual(res.parts, 4);
});

// ── server refusals ─────────────────────────────────────────────────────
check('lakeStep: a 413 on a part stops the step — one warning, NO manifest PUT', async () => {
  const w = mkWorker();
  let posts = 0;
  const F = mkFetch([
    { match: '/generator/semantics', res: () => okJson({}) },
    { match: '/api/explore/catalog', res: () => okJson({ tables: TABLES }) },
    { match: '/lake/parts', method: 'POST', res: () => {
      posts++;
      return posts === 2 ? { ok: false, status: 413, json: () => Promise.resolve({}) } : OK;
    } },
    { match: '/lake/manifest', method: 'PUT', res: () => { throw new Error('must not be called'); } },
  ]);
  const { ctx, st } = mkCtx({ worker: w, fetch: F });
  const res = await lakeStep(ctx);
  assert.strictEqual(res.error, 'part');
  assert.strictEqual(res.status, 413);
  assert.strictEqual(res.uploaded, 1, 'stopped on the SECOND part');
  assert.strictEqual(posts, 2, 'no further uploads after the refusal');
  assert.strictEqual(F.calls.filter(c => c.method === 'PUT').length, 0, 'no manifest PUT');
  assert.strictEqual(st.warns.length, 1, st.warns.join(' | '));
  assert.ok(/413/.test(st.warns[0]) && /Report index/.test(st.warns[0]), st.warns[0]);
});

check('lakeStep: a 422 on the manifest warns once and reports the failure', async () => {
  const w = mkWorker();
  const F = mkFetch([
    { match: '/generator/semantics', res: () => okJson({}) },
    { match: '/api/explore/catalog', res: () => okJson({ tables: TABLES }) },
    { match: '/lake/parts', method: 'POST', res: () => OK },
    { match: '/lake/manifest', method: 'PUT', res: () => ({ ok: false, status: 422, json: () => Promise.resolve({}) }) },
  ]);
  const { ctx, st } = mkCtx({ worker: w, fetch: F });
  const res = await lakeStep(ctx);
  assert.strictEqual(res.error, 'manifest');
  assert.strictEqual(res.status, 422);
  assert.strictEqual(st.warns.length, 1);
});

check('lakeStep: an engine error is a warning, never a throw', async () => {
  const w = mkWorker();
  const boom = {
    calls: w.calls,
    send: (t, p) => (t === 'explore:cache_probe'
      ? Promise.resolve({ error: 'Engine is not initialised yet' })
      : w.send(t, p)),
  };
  const F = mkFetch(HAPPY_ROUTES);
  const { ctx, st } = mkCtx({ worker: boom, fetch: F });
  const res = await lakeStep(ctx);
  assert.strictEqual(res.error, 'engine');
  assert.strictEqual(st.warns.length, 1);
  assert.strictEqual(F.calls.filter(c => c.method !== 'GET').length, 0, 'nothing was uploaded');
});

// ── the silent skips ────────────────────────────────────────────────────
check('lakeStep: a generator with no semantics does NOTHING (404, silent)', async () => {
  const w = mkWorker();
  const F = mkFetch([{ match: '/generator/semantics', res: () => NOT_FOUND }]);
  const { ctx, st } = mkCtx({ worker: w, fetch: F });
  const res = await lakeStep(ctx);
  assert.deepStrictEqual(res, { skipped: 'no-semantics' });
  assert.strictEqual(F.calls.length, 1, 'the catalogue is never fetched');
  assert.deepStrictEqual(w.calls, [], 'the worker is never woken');
  assert.deepStrictEqual(st.status, []);
  assert.deepStrictEqual(st.warns, []);
});

check('lakeStep: "lake": false skips before any request at all', async () => {
  const w = mkWorker();
  const F = mkFetch(HAPPY_ROUTES);
  const { ctx, st } = mkCtx({ worker: w, fetch: F, lakeEnabled: false });
  const res = await lakeStep(ctx);
  assert.deepStrictEqual(res, { skipped: 'manifest' });
  assert.deepStrictEqual(F.calls, []);
  assert.deepStrictEqual(w.calls, []);
  assert.deepStrictEqual(st.status, []);
});

check('lakeStep: a folder whose files are in no catalogue table does nothing', async () => {
  const w = mkWorker();
  const F = mkFetch([
    { match: '/generator/semantics', res: () => okJson({}) },
    { match: '/api/explore/catalog', res: () => okJson({ tables: [] }) },
  ]);
  const { ctx, st } = mkCtx({ worker: w, fetch: F });
  const res = await lakeStep(ctx);
  assert.deepStrictEqual(res, { skipped: 'no-partitions' });
  assert.deepStrictEqual(w.calls, []);
  assert.deepStrictEqual(st.warns, []);
});

check('lakeStep: an unreadable file drops out of the manifest, the rest publish', async () => {
  const w = mkWorker();
  const F = mkFetch(HAPPY_ROUTES);
  const { ctx, st } = mkCtx({
    worker: w,
    fetch: F,
    readFile: (rec) => (rec.name === 'sl_2026_02.xlsx'
      ? Promise.reject(new Error('NotFoundError'))
      : Promise.resolve(mkBuf(8))),
  });
  const res = await lakeStep(ctx);
  assert.strictEqual(res.ok, true);
  // two files failed to read (two partitions of sl_2026_02.xlsx)
  assert.strictEqual(w.calls.filter(c => c.type === 'explore:index').length, 2);
  // the run still finishes and still says 4/4 — the misses are not retried
  assert.ok(st.status.indexOf('Report index: 4/4') >= 0, st.status.join(' | '));
  assert.strictEqual(st.warns.length, 0);
});

// ── summary ─────────────────────────────────────────────────────────────
process.on('exit', () => {
  console.log((process.exitCode ? 'FAILURES' : 'ALL PASS') + ' — ' + n + ' checks');
});
