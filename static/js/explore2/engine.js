/**
 * OnePage /explore — engine v2 (workstream H1 prototype, NOT wired into the
 * live /explore path).
 *
 * The shipped engine is `static/py/_explore.py` inside the Pyodide worker:
 * python-calamine → pandas → DuckDB's pandas scan, all inside a ~200 MB wasm
 * CPython heap, single-threaded, converting every cell twice. This module is
 * the same pipeline with the interpreter deleted:
 *
 *     workbook bytes
 *       → engine/xlsx-arrow (Rust calamine, ~1.2 MB wasm)  → Arrow IPC stream
 *       → duckdb-wasm insertArrowFromIPCStream (zero-copy) → native table _ep_N
 *       → UNION ALL BY NAME view per catalogue table
 *       → Parquet in OPFS, written by DuckDB itself
 *
 * Everything the Python engine decided about *meaning* is preserved — column
 * normalisation and duplicate suffixes (in the Rust parser, `norm.rs`),
 * `__partition`/`__file`, the union's explicit type reconciliation, the SQL
 * guard. Everything it decided about *mechanism* is gone.
 *
 * Runs unchanged in three places:
 *   • a Web Worker (the eventual /explore v2 home),
 *   • the main thread (demo pages),
 *   • Node ≥ 20 (`tools/bench_engine2.mjs`, `tests/engine2/*.test.mjs`).
 *
 * The only environment-specific code is `bootDuckDB()` / `loadParser()`.
 */

import { t } from './i18n.js';
import {
  ExploreError, SQLGuardError, guardSql, checkTable, qi, lit, scan, statements,
  namesIn, MAX_SQL_CHARS, FORBIDDEN, FORBIDDEN_KEYWORDS, FORBIDDEN_CALLABLE,
} from './guard.js';

// The guard used to live in this file as a *concept* port (comments stripped
// first, statements counted second, a short forbidden list). docs/ENGINE2.md
// flagged that as the one thing to fix before a user's SQL box reached it;
// `guard.js` is now the verbatim port of `_explore.py::guard_sql`, tables and
// all. Re-exported here so the prototype's callers (bench, engine.test.mjs)
// keep working against the real guard rather than a second one.
export {
  ExploreError, SQLGuardError, guardSql, checkTable, qi, scan, statements,
  namesIn, MAX_SQL_CHARS, FORBIDDEN, FORBIDDEN_KEYWORDS, FORBIDDEN_CALLABLE,
};

// ── Constants ────────────────────────────────────────────────────────────
export const PARTITION_COL = '__partition';
export const FILE_COL = '__file';
export const REL_PREFIX = '_ep_';

/** Bump when index-time coercions change — it is part of the Parquet cache key,
 *  exactly like `_explore.PARSER_VERSION`. */
export const PARSER_VERSION = 'xlsx-arrow/0.1.0';

const IS_NODE = typeof process !== 'undefined'
  && process.versions != null && process.versions.node != null
  && typeof window === 'undefined';

// ── Type reconciliation (port of _explore.py::_is_numeric_type) ──────────
const NUMERIC_TYPES = ['TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT',
  'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'FLOAT', 'DOUBLE', 'DECIMAL'];

function isNumericType(tv) {
  return NUMERIC_TYPES.some(p => tv.startsWith(p));
}

// ── Environment bootstrap ────────────────────────────────────────────────
async function loadParser(parserBase) {
  const mod = await import(parserBase + 'xlsx_arrow.js');
  if (IS_NODE) {
    const { readFile } = await import('node:fs/promises');
    const { fileURLToPath } = await import('node:url');
    const wasmUrl = new URL(parserBase + 'xlsx_arrow_bg.wasm');
    await mod.default({ module_or_path: await readFile(fileURLToPath(wasmUrl)) });
  } else {
    await mod.default({ module_or_path: parserBase + 'xlsx_arrow_bg.wasm' });
  }
  return mod;
}

/**
 * A Web-Worker-shaped handle around a `worker_threads` worker, for Node only.
 *
 * duckdb's own `createWorker()` goes through `fetch()`, which Node will not do
 * for a `file:` URL, and `duckdb-node-*.worker.cjs` is a CommonJS bundle
 * written against the Web Worker globals — so it needs both a CJS scope (for
 * its `module.exports`) and `self`/`postMessage`/`onmessage` (for duckdb's own
 * protocol). An `eval: true` worker gives us the first; the bootstrap below
 * gives us the second. ~30 lines, and no npm dependency to vendor.
 */
async function nodeWorker(file) {
  const { Worker } = await import('node:worker_threads');
  const boot = `
    const { parentPort, workerData } = require('worker_threads');
    globalThis.self = globalThis;
    globalThis.postMessage = (m, t) => parentPort.postMessage(m, t);
    globalThis.close = () => process.exit(0);
    const listeners = [];
    globalThis.addEventListener = (t, f) => { if (t === 'message') listeners.push(f); };
    globalThis.removeEventListener = (t, f) => {
      const i = listeners.indexOf(f); if (i >= 0) listeners.splice(i, 1);
    };
    parentPort.on('message', (data) => {
      const ev = { data };
      if (typeof globalThis.onmessage === 'function') globalThis.onmessage(ev);
      for (const f of listeners.slice()) f(ev);
    });
    require(workerData.file);
  `;
  const w = new Worker(boot, { eval: true, workerData: { file } });
  const handlers = new Map();
  const on = (type, fn) => {
    if (!handlers.has(type)) handlers.set(type, []);
    handlers.get(type).push(fn);
  };
  w.on('message', (data) => {
    for (const f of handlers.get('message') || []) f({ data });
  });
  w.on('error', (err) => {
    for (const f of handlers.get('error') || []) f(err);
  });
  return {
    addEventListener: on,
    removeEventListener: (type, fn) => {
      const a = handlers.get(type) || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    },
    postMessage: (m, tv) => w.postMessage(m, tv),
    terminate: () => w.terminate(),
  };
}

export async function bootDuckDB(duckdbBase, opts) {
  if (IS_NODE) {
    const { createRequire } = await import('node:module');
    const { fileURLToPath } = await import('node:url');
    const dir = fileURLToPath(new URL(duckdbBase));
    const require = createRequire(import.meta.url);
    const duckdb = require(dir + 'duckdb-node.cjs');
    // Node has no OPFS and no cross-origin isolation, so we boot the `eh`
    // bundle — which is also what a real browser gets, so the bench measures
    // the same engine the page would.
    const worker = await nodeWorker(dir + 'duckdb-node-eh.worker.cjs');
    const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
    await db.instantiate(dir + 'duckdb-eh.wasm', null);
    return { duckdb, db, worker, bundleName: 'eh(node)' };
  }
  const duckdb = await import(duckdbBase + 'duckdb-browser.mjs');
  // Our own manifest — never getJsDelivrBundles(). The corporate deployment
  // has no CDN at runtime; this is the whole point of deploy/fetch-duckdb-wasm.py.
  const manifest = {
    mvp: {
      mainModule: duckdbBase + 'duckdb-mvp.wasm',
      mainWorker: duckdbBase + 'duckdb-browser-mvp.worker.js',
    },
    eh: {
      mainModule: duckdbBase + 'duckdb-eh.wasm',
      mainWorker: duckdbBase + 'duckdb-browser-eh.worker.js',
    },
    coi: {
      mainModule: duckdbBase + 'duckdb-coi.wasm',
      mainWorker: duckdbBase + 'duckdb-browser-coi.worker.js',
      pthreadWorker: duckdbBase + 'duckdb-browser-coi.pthread.worker.js',
    },
  };
  const bundle = await duckdb.selectBundle(manifest);
  const worker = new Worker(bundle.mainWorker);
  const logger = opts.verbose ? new duckdb.ConsoleLogger() : new duckdb.VoidLogger();
  const db = new duckdb.AsyncDuckDB(logger, worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker ?? null);
  const name = bundle.mainModule.includes('coi') ? 'coi'
    : bundle.mainModule.includes('eh') ? 'eh' : 'mvp';
  return { duckdb, db, bundleName: name };
}

// ── Result marshalling ───────────────────────────────────────────────────
export function cell(v) {
  if (v == null) return null;
  if (typeof v === 'bigint') return Number(v);
  if (v instanceof Date) return v.toISOString().replace('T', ' ').replace(/\.\d+Z?$/, '');
  if (typeof v === 'object') {
    // Arrow's Decimal128 — what DuckDB's HUGEINT arrives as, and `SUM()` over
    // a BIGINT column IS a HUGEINT. It is a Uint32Array subclass whose
    // `toJSON()` returns a JSON-QUOTED string, so the toJSON branch below
    // rendered `"20500"` in the grid, quotes included (caught in Chrome,
    // 2026-08-22). Its `toString` is the honest one.
    if (ArrayBuffer.isView(v)) {
      const s = String(v);
      const n = Number(s);
      // Past 2^53 a JS number would be a WRONG number; the digits are not.
      return Number.isFinite(n) && Math.abs(n) <= Number.MAX_SAFE_INTEGER ? n : s;
    }
    // Arrow's Vector/StructRow leak through for nested types we never build,
    // but a defensive stringify beats posting a live object across a worker.
    if (typeof v.toJSON === 'function') return v.toJSON();
    return String(v);
  }
  return v;
}

export function tableToRows(tv, limit) {
  const columns = tv.schema.fields.map(f => f.name);
  const rows = [];
  const n = Math.min(tv.numRows, limit ?? tv.numRows);
  const cols = columns.map((_, i) => tv.getChildAt(i));
  for (let r = 0; r < n; r++) {
    const row = new Array(columns.length);
    for (let c = 0; c < columns.length; c++) row[c] = cell(cols[c].get(r));
    rows.push(row);
  }
  return { columns, rows, truncated: tv.numRows > n, total: tv.numRows };
}

// ── The engine ───────────────────────────────────────────────────────────
class Engine {
  constructor(ctx) {
    Object.assign(this, ctx);
    this._seq = 0;
    this._tables = new Map();   // table -> [{rel, partition, file, rows, bytes}]
    this._opfs = null;          // null = unprobed, true/false after first try
  }

  // ── indexing ───────────────────────────────────────────────────────────
  sheetNames(bytes) {
    return Array.from(this.parser.sheet_names(bytes));
  }

  /**
   * Parse ONE partition workbook into the table's union view.
   *
   * `bytes` is consumed and never retained: after `insertArrowFromIPCStream`
   * the rows live inside DuckDB's own wasm heap, so the caller may drop its
   * ArrayBuffer immediately — the O(1)-memory discipline of the run path.
   */
  async indexFile(name, bytes, opts = {}) {
    const table = checkTable(opts.table);
    const partition = opts.partition == null ? '' : String(opts.partition);
    const file = String(opts.file || name || '');
    const sheet = opts.sheet == null ? '' : String(opts.sheet);
    const headerRow = Number.isFinite(+opts.headerRow) ? Math.max(0, +opts.headerRow | 0) : 0;

    const t0 = now();
    const ipc = this.parser.parse_sheet(bytes, sheet, headerRow);
    const tParse = now() - t0;

    const t1 = now();
    const rel = `${REL_PREFIX}${++this._seq}`;
    // `insertArrowFromIPCStream` TRANSFERS the buffer to the DuckDB worker —
    // after the call `ipc` is detached and its byteLength is 0. The Parquet
    // cache needs the same bytes for its own instance, so it gets a copy, and
    // only when it asked for one.
    const ipcBytes = ipc.length;
    const keep = opts.keepIpc ? ipc.slice() : null;
    await this.conn.insertArrowFromIPCStream(ipc, { name: rel, create: true });
    // The reserved columns are added by DuckDB, not by the parser: the parser
    // stays a pure "sheet -> Arrow" function, and ADD COLUMN … DEFAULT costs
    // one materialised column instead of a full CTAS copy of the partition.
    await this.conn.query(
      `ALTER TABLE ${qi(rel)} ADD COLUMN ${qi(PARTITION_COL)} VARCHAR DEFAULT ${lit(partition)}`);
    await this.conn.query(
      `ALTER TABLE ${qi(rel)} ADD COLUMN ${qi(FILE_COL)} VARCHAR DEFAULT ${lit(file)}`);
    const tInsert = now() - t1;

    const info = await this._describe(rel);
    const rows = Number((await this.conn.query(
      `SELECT count(*) AS n FROM ${qi(rel)}`)).getChildAt(0).get(0));

    const parts = this._tables.get(table) || [];
    // Re-indexing the same file REPLACES its partition. Otherwise every
    // re-index silently doubles every count in every answer.
    for (const old of parts.slice()) {
      if (old.file === file && old.partition === partition) {
        await this._drop(old.rel);
        parts.splice(parts.indexOf(old), 1);
      }
    }
    parts.push({ rel, partition, file, rows, sheet, bytes: ipcBytes });
    this._tables.set(table, parts);

    const t2 = now();
    await this.rebuildView(table);
    const tUnion = now() - t2;

    return {
      table, partition, file, rows, bytes: ipcBytes,
      // The sheet's own columns — `__partition`/`__file` are the engine's and
      // are reported separately, so this count lines up with the shipped
      // engine's `index_file(...)["columns"]`.
      columns: info.map(c => c.name).filter(c => c !== PARTITION_COL && c !== FILE_COL),
      partitions: parts.length,
      ms: { parse: tParse, insert: tInsert, union: tUnion },
      ipcBytes,
      // The Parquet cache writes the SAME Arrow IPC into the IO instance
      // (cache.js) — the query instance is locked out of the filesystem and
      // could not re-export it. Handed back only on request, so the ordinary
      // index path still drops the buffer at the end of this call.
      ipc: keep,
    };
  }

  /**
   * Fold an Arrow table that ALREADY carries `__partition`/`__file` into the
   * union — the cache-hit path. The rows come from the IO DuckDB's
   * `read_parquet` (see cache.js): they cross into the query instance as
   * Arrow, because the query instance is locked out of the filesystem and
   * cannot read the Parquet itself. That lock is the point, not an obstacle.
   */
  async indexArrow(name, arrowTable, opts = {}) {
    const table = checkTable(opts.table);
    const t0 = now();
    const rel = `${REL_PREFIX}${++this._seq}`;
    await this.conn.insertArrowTable(arrowTable, { name: rel, create: true });
    const info = await this._describe(rel);
    const rows = Number((await this.conn.query(
      `SELECT count(*) AS n FROM ${qi(rel)}`)).getChildAt(0).get(0));
    // `__partition`/`__file` rode along inside the Parquet, so the entry is
    // reconstructed from the DATA rather than from the caller's claim — a
    // cached partition that says it is July stays July.
    let partition = opts.partition == null ? '' : String(opts.partition);
    let file = String(opts.file || name || '');
    try {
      const tv = await this.conn.query(
        `SELECT ${qi(PARTITION_COL)} p, ${qi(FILE_COL)} f FROM ${qi(rel)} LIMIT 1`);
      if (tv.numRows) {
        partition = String(tv.getChildAt(0).get(0) ?? partition);
        file = String(tv.getChildAt(1).get(0) ?? file);
      }
    } catch { /* a Parquet without the reserved columns keeps the caller's */ }
    const parts = this._tables.get(table) || [];
    for (const old of parts.slice()) {
      if (old.file === file && old.partition === partition) {
        await this._drop(old.rel);
        parts.splice(parts.indexOf(old), 1);
      }
    }
    const bytes = Number(opts.bytes || 0);
    parts.push({ rel, partition, file, rows, sheet: opts.sheet ?? null, bytes });
    this._tables.set(table, parts);
    const t1 = now();
    await this.rebuildView(table);
    return {
      table, partition, file, rows, bytes,
      columns: info.map(c => c.name).filter(c => c !== PARTITION_COL && c !== FILE_COL),
      partitions: parts.length,
      ms: { insert: t1 - t0, union: now() - t1 },
    };
  }

  /**
   * The executor `semantics.js` needs: engine-internal SQL (no guard — never
   * reachable from a user string) plus the set of catalogue table names the
   * `sem` schema must not shadow.
   */
  semCtx() {
    return {
      exec: (sql) => this.conn.query(sql),
      rows: async (sql) => {
        const tv = await this.conn.query(sql);
        return tableToRows(tv).rows;
      },
      tableNames: new Set(this._tables.keys()),
    };
  }

  async _describe(rel) {
    const tv = await this.conn.query(`DESCRIBE SELECT * FROM ${qi(rel)}`);
    const name = tv.getChildAt(tv.schema.fields.findIndex(f => f.name === 'column_name'));
    const type = tv.getChildAt(tv.schema.fields.findIndex(f => f.name === 'column_type'));
    const out = [];
    for (let i = 0; i < tv.numRows; i++) {
      out.push({ name: String(name.get(i)), type: String(type.get(i)).toUpperCase() });
    }
    return out;
  }

  async _drop(rel) {
    await this.conn.query(`DROP TABLE IF EXISTS ${qi(rel)}`);
  }

  /**
   * Rebuild `table` as the UNION of its partitions — the port of
   * `_explore.py::_rebuild_view`, including the part that is easy to skip.
   *
   * UNION ALL **BY NAME**, not positional: partitions of one catalogue table
   * are allowed to differ in their non-core columns (that is what a table
   * *version* is), and positional UNION would either fail or, worse, line up
   * two unrelated columns that happen to share an index.
   *
   * And the types are reconciled EXPLICITLY. Partitions of a 1C export
   * disagree about a column's type all the time (numeric in July, `' '`
   * strings in August); left alone, DuckDB casts the text to the number and
   * dies on the first blank. Numbers widen to DOUBLE, anything mixed with
   * text becomes VARCHAR — the semantic layer re-parses text itself.
   */
  async rebuildView(table) {
    const parts = this._tables.get(table) || [];
    if (!parts.length) {
      await this.conn.query(`DROP VIEW IF EXISTS main.${qi(table)}`);
      this._tables.delete(table);
      return;
    }
    const perRel = new Map();
    const seen = new Map();
    for (const p of parts) {
      const cols = await this._describe(p.rel);
      perRel.set(p.rel, cols);
      for (const c of cols) {
        if (!seen.has(c.name)) seen.set(c.name, new Set());
        seen.get(c.name).add(c.type);
      }
    }
    const target = new Map();
    for (const [c, ts] of seen) {
      if (ts.size > 1) {
        target.set(c, [...ts].every(isNumericType) ? 'DOUBLE' : 'VARCHAR');
      }
    }
    const selects = parts.map(p => {
      const reps = perRel.get(p.rel)
        .filter(c => target.has(c.name))
        .map(c => `CAST(${qi(c.name)} AS ${target.get(c.name)}) AS ${qi(c.name)}`);
      return reps.length
        ? `SELECT * REPLACE (${reps.join(', ')}) FROM ${qi(p.rel)}`
        : `SELECT * FROM ${qi(p.rel)}`;
    });
    // `main.` is load-bearing: once the semantic layer sets search_path to
    // 'sem,main', an unqualified CREATE lands the catalogue view in `sem` and
    // shadows a semantic table of the same name.
    await this.conn.query(
      `CREATE OR REPLACE VIEW main.${qi(table)} AS ${selects.join(' UNION ALL BY NAME ')}`);
  }

  // ── querying ───────────────────────────────────────────────────────────
  /** Run user SQL through the guard. `limit` is applied on the result, not
   *  injected into the SQL, so an aggregate is never silently truncated
   *  before it aggregates. */
  async query(sql, limit = 1000) {
    const stmt = guardSql(sql);
    const t0 = now();
    const tv = await this.conn.query(stmt);
    const out = tableToRows(tv, limit);
    out.ms = now() - t0;
    return out;
  }

  /** Engine-internal SQL — no guard. Never reachable from a user string. */
  async raw(sql) {
    return await this.conn.query(sql);
  }

  tables() {
    const out = [];
    for (const name of [...this._tables.keys()].sort()) {
      const parts = this._tables.get(name);
      out.push({
        name,
        partitions: parts.map(p => p.partition),
        files: parts.map(p => p.file),
        rows: parts.reduce((a, p) => a + p.rows, 0),
        bytes: parts.reduce((a, p) => a + (p.bytes || 0), 0),
        // The page's "N tables · M partitions" header needs the per-partition
        // detail `_explore.tables()` returns; the flat arrays above stay for
        // the bench and the prototype's own tests.
        detail: parts.map(p => ({ key: p.partition, file: p.file, rows: p.rows, bytes: p.bytes || 0 })),
      });
    }
    return out;
  }

  async stats() {
    // duckdb-wasm exposes its own allocation through a pragma; this is the
    // honest number for "how close are we to the tab ceiling".
    let used = null, limit = null;
    try {
      const tv = await this.conn.query('SELECT * FROM pragma_database_size()');
      const idx = n => tv.schema.fields.findIndex(f => f.name === n);
      const mu = idx('memory_usage'), ml = idx('memory_limit');
      if (mu >= 0) used = String(tv.getChildAt(mu).get(0));
      if (ml >= 0) limit = String(tv.getChildAt(ml).get(0));
    } catch { /* pragma shape varies by build — never fail stats() over it */ }
    return {
      tables: this._tables.size,
      partitions: [...this._tables.values()].reduce((a, p) => a + p.length, 0),
      rows: [...this._tables.values()].reduce(
        (a, ps) => a + ps.reduce((b, p) => b + p.rows, 0), 0),
      // Approximate on purpose, like `_explore._frame_bytes`: the Arrow IPC
      // length of each partition. The page shows it as "N MB"; DuckDB's own
      // accounting is `memoryUsage` right below.
      bytes: [...this._tables.values()].reduce(
        (a, ps) => a + ps.reduce((b, p) => b + (p.bytes || 0), 0), 0),
      memoryUsage: used, memoryLimit: limit,
      bundle: this.bundleName,
      parserVersion: PARSER_VERSION,
    };
  }

  async reset() {
    for (const parts of this._tables.values()) {
      for (const p of parts) await this._drop(p.rel);
    }
    for (const name of this._tables.keys()) {
      await this.conn.query(`DROP VIEW IF EXISTS main.${qi(name)}`);
    }
    this._tables.clear();
  }

  // ── Parquet cache ──────────────────────────────────────────────────────
  /**
   * Load an extension explicitly instead of letting DuckDB auto-install it
   * mid-query. Two reasons, both learned the hard way:
   *   • the auto-install is a network fetch at QUERY time, from a Worker —
   *     on the corporate network that surfaces as "the cache is broken",
   *     not as a network error;
   *   • with `lockdown` on it fails outright, and the caller deserves to be
   *     told that its Parquet cache is off rather than to see a COPY error.
   */
  async ensureExtension(name, { force = false } = {}) {
    if (!this._ext) this._ext = new Map();
    if (this._ext.has(name) && !force) return this._ext.get(name);
    let ok = true, err = null;
    try {
      await this.conn.query(`${force ? 'FORCE ' : ''}INSTALL ${name}`);
      await this.conn.query(`LOAD ${name}`);
    } catch (e) { ok = false; err = e; }
    this._ext.set(name, ok);
    if (!ok) {
      throw new ExploreError(
        t('DuckDB extension "{p1}" unavailable{p2}: {p3}', {p1: name, p2: this.lockdown ? t(' (lockdown enabled)') : '', p3: err}));
    }
    return ok;
  }

  /** Is OPFS reachable from here? Probed once, never assumed. */
  async hasOPFS() {
    if (this._opfs !== null) return this._opfs;
    this._opfs = false;
    try {
      if (typeof navigator !== 'undefined' && navigator.storage
          && typeof navigator.storage.getDirectory === 'function') {
        await navigator.storage.getDirectory();
        this._opfs = true;
      }
    } catch { this._opfs = false; }
    return this._opfs;
  }

  /**
   * Write one catalogue table to Parquet — through DuckDB's own COPY, so the
   * rows never round-trip through JS. On a browser with OPFS the file lands in
   * origin-private storage and survives the tab; everywhere else (Node, a
   * browser with OPFS blocked) it falls back to a wasm-memory buffer that the
   * caller may persist itself.
   *
   * Returns `{ name, opfs, bytes, ms }` — `bytes` is null for the OPFS path
   * (the point of OPFS is that the bytes never enter JS).
   */
  async toParquet(table, opts = {}) {
    checkTable(table);
    await this.ensureExtension('parquet');
    const t0 = now();
    const opfs = opts.opfs ?? await this.hasOPFS();
    if (opfs) {
      const name = `opfs://${opts.name || `${table}.parquet`}`;
      await this.db.registerOPFSFileName(name);
      await this.conn.query(`COPY main.${qi(table)} TO ${lit(name)} (FORMAT PARQUET)`);
      return { name, opfs: true, bytes: null, ms: now() - t0 };
    }
    if (IS_NODE) {
      // In Node, duckdb-wasm's runtime resolves a bare filename against the
      // REAL filesystem (NODE_FS), which quietly wins over a registered
      // buffer of the same name — measured: a stale `zakazy.parquet` left in
      // the cwd by an earlier run was what `copyFileToBuffer` returned. So on
      // Node we go through a temp file explicitly and never leave one behind.
      const { tmpdir } = await import('node:os');
      const { join } = await import('node:path');
      const { readFile, unlink } = await import('node:fs/promises');
      const path = join(tmpdir(), `op-explore2-${Date.now()}-${++this._seq}.parquet`);
      await this.conn.query(`COPY main.${qi(table)} TO ${lit(path)} (FORMAT PARQUET)`);
      const buf = new Uint8Array(await readFile(path));
      await unlink(path).catch(() => {});
      return { name: path, opfs: false, bytes: buf, ms: now() - t0 };
    }
    const name = opts.name || `${table}.parquet`;
    await this.db.registerEmptyFileBuffer(name);
    await this.conn.query(`COPY main.${qi(table)} TO ${lit(name)} (FORMAT PARQUET)`);
    const buf = await this.db.copyFileToBuffer(name);
    await this.db.dropFile(name);
    return { name, opfs: false, bytes: buf, ms: now() - t0 };
  }

  /**
   * Read a Parquet partition back as a `_ep_N` relation and fold it into
   * `table`'s union — the cache-hit path that lets a re-opened /explore skip
   * the workbook entirely (the same trick `_explore.index_cached` plays).
   *
   * `ref` is either `{ name: 'opfs://…' }` or `{ bytes: Uint8Array }`.
   */
  async fromParquet(table, ref, opts = {}) {
    checkTable(table);
    await this.ensureExtension('parquet');
    const t0 = now();
    let name = ref.name;
    let temp = false;
    let tmpPath = null;
    if (!name && IS_NODE) {
      // Same NODE_FS caveat as toParquet: a real temp file, not a buffer.
      const { tmpdir } = await import('node:os');
      const { join } = await import('node:path');
      const { writeFile } = await import('node:fs/promises');
      tmpPath = join(tmpdir(), `op-explore2-in-${Date.now()}-${this._seq + 1}.parquet`);
      await writeFile(tmpPath, ref.bytes);
      name = tmpPath;
    } else if (!name) {
      name = opts.name || `_ep_in_${this._seq + 1}.parquet`;
      await this.db.registerFileBuffer(name, ref.bytes);
      temp = true;
    } else if (name.startsWith('opfs://')) {
      await this.db.registerOPFSFileName(name);
    }
    const rel = `${REL_PREFIX}${++this._seq}`;
    await this.conn.query(
      `CREATE OR REPLACE TABLE ${qi(rel)} AS SELECT * FROM read_parquet(${lit(name)})`);
    if (temp) await this.db.dropFile(name);
    if (tmpPath) {
      const { unlink } = await import('node:fs/promises');
      await unlink(tmpPath).catch(() => {});
    }
    const rows = Number((await this.conn.query(
      `SELECT count(*) AS n FROM ${qi(rel)}`)).getChildAt(0).get(0));
    // __partition/__file rode along inside the Parquet file, so the entry is
    // reconstructed from the data rather than from the caller's claim.
    let partition = opts.partition ?? '';
    let file = opts.file ?? '';
    try {
      const tv = await this.conn.query(
        `SELECT ${qi(PARTITION_COL)} p, ${qi(FILE_COL)} f FROM ${qi(rel)} LIMIT 1`);
      if (tv.numRows) {
        partition = String(tv.getChildAt(0).get(0) ?? partition);
        file = String(tv.getChildAt(1).get(0) ?? file);
      }
    } catch { /* a Parquet without the reserved columns keeps the caller's */ }
    const parts = this._tables.get(table) || [];
    for (const old of parts.slice()) {
      if (old.file === file && old.partition === partition) {
        await this._drop(old.rel);
        parts.splice(parts.indexOf(old), 1);
      }
    }
    parts.push({ rel, partition, file, rows, sheet: null });
    this._tables.set(table, parts);
    await this.rebuildView(table);
    return { table, partition, file, rows, ms: now() - t0 };
  }

  async close() {
    try { await this.conn.close(); } catch { /* already gone */ }
    try { await this.db.terminate(); } catch { /* already gone */ }
    if (IS_NODE && this.worker) { try { await this.worker.terminate(); } catch { /* */ } }
  }
}

function now() {
  return (typeof performance !== 'undefined' ? performance.now() : Date.now());
}

/**
 * Boot the engine.
 *
 *   base        – URL prefix under which static/ is served. In Node pass a
 *                 `file://` URL (the bench does).
 *   duckdbBase  – defaults to `${base}duckdb-wasm/`
 *   parserBase  – defaults to `${base}xlsx-arrow/`
 *   lockdown    – set DuckDB's `enable_external_access=false` on boot. It is
 *                 the belt to guardSql's braces — but DuckDB makes the setting
 *                 one-way and it also blocks COPY, so `toParquet`/`fromParquet`
 *                 stop working. Off by default; see docs/ENGINE2.md.
 *   memoryLimit – e.g. '512MB', matching _explore.MEMORY_LIMIT.
 */
export async function createEngine(opts = {}) {
  const base = opts.base || '/static/';
  const duckdbBase = opts.duckdbBase || base + 'duckdb-wasm/';
  const parserBase = opts.parserBase || base + 'xlsx-arrow/';

  const [parser, ddb] = await Promise.all([
    loadParser(parserBase),
    bootDuckDB(duckdbBase, opts),
  ]);
  const conn = await ddb.db.connect();

  if (opts.memoryLimit) {
    await conn.query(`SET memory_limit='${String(opts.memoryLimit).replace(/'/g, '')}'`);
  }
  // Point DuckDB's extension installer at OUR origin.
  //
  // Measured 2026-08-22 and easy to miss: `parquet` is NOT statically linked
  // into duckdb-{mvp,eh}.wasm 1.32.0. `duckdb_extensions()` reports it
  // NOT_INSTALLED at boot and the first `COPY … (FORMAT PARQUET)` silently
  // auto-installs it from extensions.duckdb.org — a CDN fetch, from a Worker,
  // at query time. `deploy/fetch-duckdb-wasm.py` mirrors the repository layout
  // under static/duckdb-ext/; this is what makes the mirror get used.
  //
  // Skipped for a `file:` base (Node), where DuckDB's runtime cannot fetch at
  // all — the Node tests point this at a local http server instead.
  let extensionRepository = opts.extensionRepository ?? null;
  if (extensionRepository === null && !/^file:/i.test(base)) {
    extensionRepository = base + 'duckdb-ext/';
  }
  if (extensionRepository) {
    // No trailing slash: DuckDB appends `/v{version}/{platform}/{name}…`
    // itself, and a doubled slash is one more thing for a reverse proxy to
    // normalise differently than we expect.
    extensionRepository = String(extensionRepository).replace(/'/g, '').replace(/\/+$/, '');
    await conn.query(`SET custom_extension_repository='${extensionRepository}'`);
  }

  let lockdown = false;
  if (opts.lockdown) {
    try {
      await conn.query('SET enable_external_access=false');
      lockdown = true;
    } catch (e) {
      // Recorded, not swallowed: the caller has to know whether the second
      // line of defence is actually there.
      lockdown = false;
    }
  }
  return new Engine({
    parser, db: ddb.db, conn, duckdb: ddb.duckdb, worker: ddb.worker,
    bundleName: ddb.bundleName, lockdown, extensionRepository,
  });
}

export default createEngine;
