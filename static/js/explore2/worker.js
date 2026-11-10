/**
 * OnePage /explore — engine v2 worker.
 *
 * The drop-in twin of `gen-worker.js`'s explore mode: SAME postMessage
 * protocol, different engine underneath. `explore.js` picks between the two by
 * URL and changes nothing else — that swap is the whole point of keeping
 * `engine.js` free of the DOM and of Pyodide.
 *
 *   gen-worker.js  Pyodide → python-calamine → pandas → DuckDB (one instance,
 *                  enable_external_access=false, plus `_io_conn` for Parquet)
 *   this file      xlsx-arrow (Rust/wasm) → Arrow IPC → duckdb-wasm (two
 *                  instances: a LOCKED query instance, and an IO instance that
 *                  owns the Parquet cache — see cache.js)
 *
 * Protocol (page -> worker), identical to gen-worker.js:
 *   {type:'explore:init',        pyodideBase, staticBase, catalog}
 *   {type:'explore:index',       id, files:[…], final}
 *   {type:'explore:cache_probe', id, files:[…]}
 *   {type:'explore:cache_clear', id}
 *   {type:'explore:cache_export', id, keys:[…]}   -- phase 9.2: portable cache
 *   {type:'explore:cache_import', id, key, buf}
 *   {type:'explore:sql',         id, sql, limit}
 *   {type:'explore:intent',      id, intent}
 *   {type:'explore:export',      id, sql, name}
 *   {type:'explore:check',       id, checks}
 *   {type:'explore:reset'}
 * worker -> page: 'explore:ready' | 'progress' | 'warning' | 'explore:indexed'
 *                 | 'explore:cache_probed' | 'explore:cache_cleared'
 *                 | 'explore:cache_exported' | 'explore:cache_imported'
 *                 | 'explore:result' | 'explore:intent_result'
 *                 | 'explore:file' | 'explore:check_result' | 'explore:error'
 *
 * What is v1-only, deliberately, and visible to the page rather than silent:
 *   • "Check folder" — `explore:check` answers `{findings: [], note}`.
 *     The checks live in `_folder_check.py` and stay on the Pyodide engine
 *     for now; a folder check under `?engine=2` says so instead of
 *     pretending the folder is clean.
 *   • the in-DuckDB interrupt timer. duckdb-wasm has `cancelSent()` but no
 *     way to pre-empt a statement that never yields a batch, so the deadline
 *     is enforced between batches (exactly like `_explore.run_sql`'s fetch
 *     deadline) and the page's `killWorker` remains the hard stop.
 */

import { t } from './i18n.js';
import { createEngine, cell, PARSER_VERSION } from './engine.js';
import { guardSql, checkTable, namesIn, ExploreError } from './guard.js';
import { SemanticLayer } from './semantics.js';
import { ParquetCache, cacheKey } from './cache.js';
import { writeXlsx } from './xlsx.js';

// ── Limits (the same numbers as _explore.py) ─────────────────────────────
const DEFAULT_LIMIT = 1000;
const MAX_LIMIT = 10000;
const EXPORT_MAX_ROWS = 100000;
const QUERY_TIMEOUT_S = 30.0;
const MEMORY_LIMIT = '512MB';
const FETCH_CHUNK_ROWS = 2048;
const WASM_WARN_BYTES = 1.6 * 1024 ** 3;

// ── State ────────────────────────────────────────────────────────────────
let engine = null;
let cache = null;
const sem = new SemanticLayer();
let booting = null;
let staticBase = '/static/';
const EXPECTED = new Map();   // table name -> [partition key, …] (the catalogue)
const DISPLAY = new Map();    // table name -> display_name
let byId = new Map();         // table_id -> catalogue table
let peakBytes = 0;

function now() { return (typeof performance !== 'undefined' ? performance.now() : Date.now()); }

function short(e) {
  const txt = String((e && e.message) || e || '').trim().split('\n').filter(l => l.trim());
  return (txt.length ? txt[0] : t('error')).slice(0, 300);
}

function progress(stage, done, total, msg) {
  self.postMessage({ type: 'progress', stage, done, total, msg: msg || '' });
}

/** The catalogue snapshot indexes tables by `table_id`; the engine names its
 *  views by `name`. The only place the two are bridged — same as
 *  gen-worker.js::exploreTableName. */
function tableName(f) {
  if (f.table) return String(f.table);
  const tv = byId.get(String(f.table_id || ''));
  return tv ? String(tv.name || '') : '';
}

// ── Catalogue snapshot (port of _explore.set_catalog) ────────────────────
function setCatalog(catalog) {
  EXPECTED.clear();
  DISPLAY.clear();
  byId = new Map();
  const tables = (catalog && catalog.tables) || [];
  for (const tv of tables) {
    if (!tv || typeof tv !== 'object') continue;
    if (tv.table_id) byId.set(String(tv.table_id), tv);
    let name;
    try { name = checkTable(tv.name); } catch { continue; }
    const keys = [];
    for (const p of (tv.partitions || [])) {
      const raw = (p && typeof p === 'object') ? p.key : p;
      const key = raw == null ? '' : String(raw);
      if (!keys.includes(key)) keys.push(key);
    }
    EXPECTED.set(name, keys);
    DISPLAY.set(name, String(tv.display_name || name));
  }
  sem.setSemantics((catalog && catalog.semantics) || []);
}

/** The shape `_explore.tables()` returns — the page's header and the staleness
 *  badge both read from here. */
function tablesOut() {
  const out = [];
  for (const tv of engine.tables()) {
    const expected = EXPECTED.get(tv.name) || [];
    const have = tv.detail.map(p => p.key);
    out.push({
      name: tv.name,
      display_name: DISPLAY.get(tv.name) || tv.name,
      partitions: tv.detail.map(p => ({ key: p.key, file: p.file, rows: p.rows })),
      rows: tv.rows,
      bytes: tv.bytes,
      expected: expected.length ? expected.length : tv.detail.length,
      missing: expected.filter(k => !have.includes(k)),
    });
  }
  return out;
}

/** "N MB" in a DuckDB pragma reads as "33.9 MiB"; the page wants bytes. */
function parseSize(s) {
  const m = /^([\d.]+)\s*([KMGT]?)i?B?$/i.exec(String(s || '').trim());
  if (!m) return 0;
  const mult = { '': 1, K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4 };
  return Math.round(parseFloat(m[1]) * (mult[m[2].toUpperCase()] || 1));
}

async function statsOut() {
  const st = await engine.stats();
  const used = parseSize(st.memoryUsage);
  if (used > peakBytes) peakBytes = used;
  return {
    tables: st.tables, partitions: st.partitions, rows: st.rows, bytes: st.bytes,
    // engine-2 extras — the page ignores what it does not know, and the
    // Chrome harness reads them.
    engine: 2, bundle: st.bundle, parserVersion: PARSER_VERSION,
    memoryUsage: st.memoryUsage, memoryLimit: st.memoryLimit,
    duckdbBytes: used, lockdown: !!engine.lockdown,
    cacheMode: cache ? cache.mode : null,
  };
}

// ── Boot ─────────────────────────────────────────────────────────────────
async function boot() {
  progress('boot', 0, 1, t('Loading query engine'));
  // `lockdown: true` is the belt to guardSql's braces, and the reason the
  // Parquet cache needs its own instance: `enable_external_access=false` is
  // one-way and blocks COPY/read_parquet along with everything else.
  engine = await createEngine({
    base: staticBase, lockdown: true, memoryLimit: MEMORY_LIMIT,
  });
  cache = new ParquetCache({
    duckdbBase: staticBase + 'duckdb-wasm/',
    extensionRepository: staticBase + 'duckdb-ext/',
    memoryLimit: MEMORY_LIMIT,
  });
  progress('boot', 1, 1, '');
}

// ── Execution (port of _explore._execute / run_sql / _context) ───────────
function emptyResult(error) {
  return {
    columns: [], rows: [], total: 0, ms: 0, partitions_used: [],
    partitions_expected: 0, missing_partitions: [], truncated: false,
    incomplete: false, error: error ?? null,
  };
}

/** Which indexed tables this statement names, semantic views resolved to the
 *  catalogue tables they stand on — the port of `_explore._referenced`. */
function referenced(stmt) {
  const names = engine.tables().map(tv => tv.name);
  const hits = namesIn(stmt, names);
  for (const vname of sem.semanticNames()) {
    if (!namesIn(stmt, [vname]).length) continue;
    for (const tv of sem.depsFor(vname)) {
      if (names.includes(tv) && !hits.includes(tv)) hits.push(tv);
    }
  }
  return hits;
}

/** Partition provenance for the result badge. */
function context(stmt) {
  const used = [];
  const missing = [];
  let expected = 0;
  const detail = new Map(engine.tables().map(tv => [tv.name, tv.detail]));
  for (const name of referenced(stmt)) {
    const parts = detail.get(name) || [];
    const exp = EXPECTED.get(name) && EXPECTED.get(name).length
      ? EXPECTED.get(name) : parts.map(p => p.key);
    expected += exp.length;
    const have = parts.map(p => p.key);
    for (const p of parts) used.push({ table: name, key: p.key, file: p.file });
    for (const k of exp) if (!have.includes(k)) missing.push({ table: name, key: k });
  }
  return {
    partitions_used: used,
    partitions_expected: expected || used.length,
    missing_partitions: missing,
  };
}

class ExploreTimeout extends ExploreError {}

/**
 * Run `stmt`, return {columns, rows[<=limit], total, truncated}.
 *
 * Streamed on purpose: `conn.send()` hands back RecordBatches, so a `SELECT *`
 * over a folder that does not fit in a tab is stopped at `limit + 1` rows
 * instead of being materialised whole — the port of `_explore._execute`'s
 * `fetchmany` loop, deadline included. `limit + 1` answers "is there more?"
 * without a second execution when there isn't; only when there IS more does a
 * `count(*)` wrapper run.
 */
async function execute(stmt, limit, timeoutS = QUERY_TIMEOUT_S) {
  const conn = engine.conn;
  const deadline = now() + (timeoutS || 0) * 1000;
  let columns = [];
  const rows = [];
  let reader = null;
  try {
    reader = await conn.send(stmt, true);
    // `send()` hands back an AsyncRecordBatchStreamReader that has NOT read its
    // schema yet — `reader.schema` is undefined until `open()`. Measured in
    // Chrome: without this every query died with "Cannot read properties of
    // undefined (reading 'fields')", which reads like a page bug and is a
    // one-line contract detail.
    await reader.open();
    columns = reader.schema.fields.map(f => f.name);
    for await (const batch of reader) {
      const cols = columns.map((_, i) => batch.getChildAt(i));
      const n = batch.numRows;
      for (let r = 0; r < n && rows.length <= limit; r++) {
        const row = new Array(columns.length);
        for (let c = 0; c < columns.length; c++) row[c] = cell(cols[c].get(r));
        rows.push(row);
      }
      if (timeoutS && now() > deadline) {
        throw new ExploreTimeout(t('Query stopped: over {p1} s. Add filter or restrict partitions.', {p1: Math.round(timeoutS)}));
      }
      if (rows.length > limit) break;
    }
  } finally {
    // A reader abandoned mid-stream leaves a pending query on the connection;
    // the next statement would answer with the tail of this one.
    try { await conn.cancelSent(); } catch { /* nothing pending */ }
  }
  const truncated = rows.length > limit;
  if (truncated) rows.length = limit;
  let total = rows.length;
  if (truncated) {
    total = null;                    // honest unknown beats a wrong number
    try {
      const tv = await conn.query(`SELECT count(*) FROM (${stmt}) AS _op_total`);
      total = Number(tv.getChildAt(0).get(0));
    } catch { total = null; }
  }
  return { columns, rows, total, truncated };
}

const ERROR_HINTS = [
  ['Unimplemented type for cast',
    t('One column has mixed data types across files (e.g. time and datetime). Re-index folder; if it persists, contact admin.')],
  ['Failed to cast',
    t('Failed to unify column values across files. Try selecting columns explicitly or add partition filter.')],
  ['does not exist',
    t('Table or column not found. Check name (case-sensitive, quoted if needed).')],
  ['not found in FROM clause',
    t('Column not found in selected table. Check name in Column list above builder.')],
  ['Parser Error',
    t('SQL syntax error. Check query near the location below.')],
  ['Out of Memory',
    t('Query ran out of browser memory. Narrow results: partition filter, fewer columns, smaller LIMIT.')],
  ['Binder Error',
    t('Query references missing name in data. Check column and table names.')],
];

function humanize(msg) {
  for (const [needle, human] of ERROR_HINTS) {
    if (msg.includes(needle)) return `${human} [${msg}]`;
  }
  return msg;
}

/** Guard, run, and answer WITH ITS OWN CONTEXT — `incomplete` is a field of
 *  the result, never a log line. */
async function runSql(sql, limit) {
  const t0 = now();
  let stmt;
  try {
    stmt = guardSql(sql);
  } catch (e) {
    return emptyResult(short(e));
  }
  let lim = parseInt(limit, 10);
  if (!Number.isFinite(lim) || lim <= 0) lim = DEFAULT_LIMIT;
  lim = Math.max(1, Math.min(lim, MAX_LIMIT));
  const ctx = context(stmt);
  try {
    const { columns, rows, total, truncated } = await execute(stmt, lim);
    return Object.assign({
      columns, rows, total,
      ms: Math.round(now() - t0), truncated, error: null,
    }, ctx, { incomplete: ctx.missing_partitions.length > 0 || truncated });
  } catch (e) {
    const out = emptyResult(e instanceof ExploreError ? short(e) : humanize(short(e)));
    Object.assign(out, ctx);
    out.ms = Math.round(now() - t0);
    out.incomplete = true;
    return out;
  }
}

// ── Indexing ─────────────────────────────────────────────────────────────
async function indexOne(f, timings) {
  const table = tableName(f);
  if (!table) throw new ExploreError(t('file not linked to catalogue table'));
  const partition = f.partition === null || f.partition === undefined ? '' : String(f.partition);
  const key = f.cache_key ? String(f.cache_key) : null;

  if (f.cached && key) {
    // Served from the Parquet cache: the page never read the workbook.
    const t0 = now();
    const arrow = await cache.get(key);
    timings.cache_read += Math.round(now() - t0);
    const out = await engine.indexArrow(f.name, arrow, {
      table, partition, file: f.name, sheet: f.sheet || null,
    });
    timings.register += Math.round(out.ms.insert);
    timings.view += Math.round(out.ms.union);
    sem.markDirty();
    return { cached: true };
  }

  const out = await engine.indexFile(f.name, new Uint8Array(f.buf), {
    table, partition, file: f.name, sheet: f.sheet || '', headerRow: f.header_row || 0,
    keepIpc: !!key,
  });
  timings.parse += Math.round(out.ms.parse);
  timings.register += Math.round(out.ms.insert);
  timings.view += Math.round(out.ms.union);
  sem.markDirty();
  if (key && out.ipc) {
    const t0 = now();
    await cache.put(key, out.ipc, partition, f.name);
    timings.cache_write += Math.round(now() - t0);
  }
  return { cached: false };
}

// ── Message handling ─────────────────────────────────────────────────────
async function handle(msg) {
  if (msg.type === 'explore:reset') {
    if (engine) await engine.reset();
    sem.markDirty();
    self.postMessage({
      type: 'explore:ready', tables: [],
      stats: { tables: 0, partitions: 0, rows: 0, bytes: 0, engine: 2 },
    });
    return;
  }

  if (msg.type === 'explore:init') {
    staticBase = msg.staticBase || staticBase;
    // Idempotent: the page re-inits after re-picking a folder, and booting a
    // second pair of DuckDB instances in one worker would double the wasm.
    if (!engine) {
      if (!booting) booting = boot();
      try { await booting; } finally { booting = null; }
    }
    setCatalog(msg.catalog || { tables: [] });
    self.postMessage({
      type: 'explore:ready', tables: tablesOut(), stats: await statsOut(),
    });
    return;
  }

  if (!engine) throw new ExploreError(t('Engine not yet initialised — reconnect folder.'));

  if (msg.type === 'explore:index') {
    const files = msg.files || [];
    const errors = [];
    let indexed = 0;
    let cached = 0;
    // The same key names v1 reports, so explore.js's `spent` accumulator and
    // its summary line work unchanged. `fs_write`/`coerce` are structurally
    // zero here: there is no /inbox copy and no second coercion pass — the
    // Rust parser types the cells once, on the way to Arrow.
    const timings = {
      fs_write: 0, parse: 0, coerce: 0, register: 0, view: 0,
      cache_write: 0, cache_read: 0,
    };
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      progress('index', i, files.length, f.name);
      try {
        const r = await indexOne(f, timings);
        indexed += 1;
        if (r.cached) cached += 1;
      } catch (e) {
        // One unreadable workbook must not cost the whole folder: record it,
        // keep going, and let the page show a thinned index as thinned.
        errors.push({ name: f.name, error: short(e) });
      }
      f.buf = null;
      const st = await engine.stats();
      const used = parseSize(st.memoryUsage);
      if (used > peakBytes) peakBytes = used;
      if (used > WASM_WARN_BYTES) {
        self.postMessage({ type: 'warning', msg: t('Much data in memory — exclude some partitions.') });
      }
    }
    progress('index', files.length, files.length, '');
    // Semantic views only once per index RUN, on the page's last message —
    // rebuilding them after every file re-ran the definitions' aggregates over
    // the whole index each time (quadratic).
    let semantics = null;
    const ts = now();
    if (msg.final) semantics = await sem.status(engine.semCtx());
    timings.semantics = Math.round(now() - ts);
    self.postMessage({
      type: 'explore:indexed', id: msg.id || null, indexed, cached, errors,
      tables: tablesOut(),
      stats: await statsOut(),
      cache: await cache.stats(),
      semantics,
      timings,
      peakWasmBytes: peakBytes,
    });
    return;
  }

  if (msg.type === 'explore:cache_probe') {
    // Keys are computed HERE so page and worker can never disagree on the
    // recipe — the same reason gen-worker.js computes them.
    const recs = msg.files || [];
    const keys = [];
    for (const r of recs) {
      keys.push(await cacheKey(r.name, r.size || 0, r.mtime_ms || 0, r.sheet || '', r.header_row || 0));
    }
    const present = await cache.probe(keys);
    self.postMessage({
      type: 'explore:cache_probed', id: msg.id || null,
      keys, cached: keys.map(k => !!present[k]),
      // Phase 9.2: a partition manifest is only importable across browsers
      // when both parsers coerce types the same way — v2's PARSER_VERSION is
      // its own, and saying so is what lets the page refuse a foreign one.
      parser_version: PARSER_VERSION,
      stats: await cache.stats(),
    });
    return;
  }

  if (msg.type === 'explore:cache_export') {
    // The Parquet bytes of partitions THIS browser already parsed, so another
    // user can import them instead of re-reading the workbooks. Buffers are
    // TRANSFERRED; `slice()` first because the cache keeps its own copy in
    // 'memory' mode and a transfer would detach it.
    const got = await cache.exportKeys((msg.keys || []).map(String));
    const parts = [];
    const transfer = [];
    for (const p of got) {
      const ab = p.bytes.slice().buffer;
      parts.push({ key: p.key, buf: ab });
      transfer.push(ab);
    }
    self.postMessage({ type: 'explore:cache_exported', id: msg.id || null, parts }, transfer);
    return;
  }

  if (msg.type === 'explore:cache_import') {
    // Bytes from another user: validated (Parquet magic + a real DESCRIBE
    // through the IO instance) before anything is written under the key.
    const out = await cache.importKey(String(msg.key || ''), new Uint8Array(msg.buf || new ArrayBuffer(0)));
    self.postMessage({
      type: 'explore:cache_imported', id: msg.id || null,
      ok: !!out.ok, bytes: out.bytes || 0, error: out.error || null,
    });
    return;
  }

  if (msg.type === 'explore:cache_clear') {
    const out = await cache.clear();
    self.postMessage({ type: 'explore:cache_cleared', id: msg.id || null, removed: out.removed });
    return;
  }

  if (msg.type === 'explore:intent') {
    // A validated intent -> SQL text. Never executes: the page puts the SQL in
    // the editor and the run is a separate click.
    const out = await sem.intentSql(msg.intent || {}, engine.semCtx());
    out.type = 'explore:intent_result';
    out.id = msg.id || null;
    self.postMessage(out);
    return;
  }

  if (msg.type === 'explore:sql') {
    const out = await runSql(String(msg.sql || ''), msg.limit || DEFAULT_LIMIT);
    out.type = 'explore:result';
    out.id = msg.id || null;
    self.postMessage(out);
    return;
  }

  if (msg.type === 'explore:check') {
    // v1-only for now: the checks are `_folder_check.py`, which needs the
    // Pyodide engine. Answering with an empty finding list AND a note is the
    // honest shape — a folder check that silently reports "no findings"
    // because it never ran is worse than one that says it did not run.
    self.postMessage({
      type: 'explore:check_result', id: msg.id || null, findings: [],
      note: t('Folder check only works with engine v1 — open /explore without ?engine=2.'),
    });
    return;
  }

  if (msg.type === 'explore:export') {
    const stmt = guardSql(String(msg.sql || ''));
    const { columns, rows, truncated } = await execute(stmt, EXPORT_MAX_ROWS);
    const bytes = await writeXlsx(columns, rows, {
      sheet: t('Result {ctx:2}'),
      note: truncated ? t('Showing first {p1} rows — result truncated.', {p1: EXPORT_MAX_ROWS}) : null,
    });
    const ab = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    self.postMessage({
      type: 'explore:file', id: msg.id || null,
      name: msg.name || 'explore.xlsx', buf: ab,
    }, [ab]);
    return;
  }
}

self.onmessage = async (e) => {
  const msg = (e && e.data) || {};
  try {
    await handle(msg);
  } catch (err) {
    self.postMessage({
      type: 'explore:error', id: msg.id || null, message: short(err),
    });
  }
};
