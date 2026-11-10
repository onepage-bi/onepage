/**
 * OnePage /explore engine v2 — the partition cache (Parquet in OPFS).
 *
 * The shipped engine writes parsed partitions as Parquet under
 * `/cache/explore` (OPFS mounted into Pyodide's FS) through a SECOND DuckDB
 * connection — `_explore._io_conn` — because the query connection is opened
 * with `enable_external_access=false` and may not touch a file. v2 keeps that
 * split, but harder: duckdb-wasm has no shared in-process catalogue, so the IO
 * side is a whole second `AsyncDuckDB` instance.
 *
 *     parse → Arrow IPC ──┬──→ query instance (LOCKED)  → the union view
 *                         └──→ IO instance (unlocked)   → COPY … PARQUET → OPFS
 *
 *     cache hit:  OPFS → IO instance read_parquet → Arrow → query instance
 *
 * The Arrow hop on the read path is not a detour, it is the lock working: the
 * query instance is not allowed to open the Parquet, so the rows arrive as
 * Arrow the same way a freshly parsed workbook does.
 *
 * Storage, in order of preference — probed once, never assumed, and REPORTED
 * (`mode`) so the page can say why a re-index was slow:
 *
 *   'opfs'    DuckDB writes straight into origin-private storage through
 *             `registerOPFSFileName` + `opfs://`. Nothing crosses JS.
 *   'opfs-js' DuckDB writes to a wasm-memory buffer, we persist the bytes with
 *             the plain OPFS API. Same durability, one copy through JS.
 *   'memory'  no OPFS at all (a browser with storage blocked, Node): the cache
 *             lives for the life of the worker and nothing survives a reload.
 *
 * Keys are `cacheKey(name, size, mtime, sheet, headerRow)` — the same recipe
 * as `_explore.cache_key` with v2's own PARSER_VERSION, so the two engines
 * never read each other's Parquet.
 */
import { t } from './i18n.js';
import { qi, lit, checkTable } from './guard.js';
import { bootDuckDB, PARTITION_COL, FILE_COL, PARSER_VERSION } from './engine.js';

const DIR = 'op-explore2-cache';
const MAX_BYTES = 2 * 1024 ** 3;     // the same 2 GB LRU ceiling as v1
const KEY_RE = /^[0-9a-f]{40}$/;
// Phase 9.2: an imported key was minted by ANOTHER browser's engine — accept
// any hex key of a sane width, the same shape `_explore.cache_export` validates.
const PORTABLE_KEY_RE = /^[a-f0-9]{16,64}$/;

/** sha1 of the same fields `_explore.cache_key` hashes. WebCrypto is async,
 *  which is why every caller of this awaits. */
export async function cacheKey(name, size, mtimeMs, sheet, headerRow) {
  const raw = `${PARSER_VERSION}|${name}|${Math.trunc(size || 0)}|${Math.trunc(mtimeMs || 0)}`
    + `|${sheet || ''}|${Math.trunc(headerRow || 0)}`;
  const buf = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(raw));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
}

export class ParquetCache {
  constructor(opts = {}) {
    this.duckdbBase = opts.duckdbBase;
    this.extensionRepository = opts.extensionRepository || null;
    this.memoryLimit = opts.memoryLimit || null;
    this.mode = null;          // set on the first ensure()
    this.error = null;         // why the cache is off, if it is
    this._db = null;
    this._conn = null;
    this._dir = null;          // OPFS directory handle
    this._mem = new Map();     // key -> Uint8Array (the 'memory' mode)
    this._seq = 0;
    this._booting = null;
  }

  /** Probe OPFS and boot the IO DuckDB. Idempotent, and never throws: a cache
   *  that cannot start only costs the next index its speed. */
  async ensure() {
    if (this.mode) return this.mode;
    if (!this._booting) this._booting = this._boot();
    await this._booting;
    return this.mode;
  }

  async _boot() {
    try {
      const root = await navigator.storage.getDirectory();
      this._dir = await root.getDirectoryHandle(DIR, { create: true });
    } catch (e) {
      this._dir = null;
      this.error = t('OPFS unavailable: {p1}', {p1: String(e && e.message || e).slice(0, 200)});
    }
    try {
      const ddb = await bootDuckDB(this.duckdbBase, {});
      this._db = ddb.db;
      this._conn = await ddb.db.connect();
      if (this.memoryLimit) {
        await this._conn.query(`SET memory_limit='${String(this.memoryLimit).replace(/'/g, '')}'`);
      }
      if (this.extensionRepository) {
        await this._conn.query(
          `SET custom_extension_repository='${String(this.extensionRepository).replace(/'/g, '').replace(/\/+$/, '')}'`);
      }
      // `parquet` is NOT statically linked into duckdb-{mvp,eh}.wasm 1.32.0.
      // Load it EXPLICITLY, from our own mirror, before any COPY: the
      // auto-install is a network fetch at query time from inside a Worker,
      // which on the corporate network surfaces as "the cache is broken"
      // rather than as an error anybody can see.
      await this._conn.query('INSTALL parquet');
      await this._conn.query('LOAD parquet');
    } catch (e) {
      this.mode = 'off';
      this.error = t('Partition cache disabled: {p1}', {p1: String(e && e.message || e).slice(0, 200)});
      return;
    }
    if (!this._dir) { this.mode = 'memory'; return; }
    // Does DuckDB's own OPFS path work in THIS browser? Write one byte-sized
    // Parquet through `opfs://` and read it back. Measured, not assumed — the
    // prototype's OPFS branch had never executed anywhere.
    try {
      const probe = `opfs://${DIR}/_probe.parquet`;
      await this._db.registerOPFSFileName(probe);
      await this._conn.query(`COPY (SELECT 1 AS x) TO ${lit(probe)} (FORMAT PARQUET)`);
      const tv = await this._conn.query(`SELECT count(*) AS n FROM read_parquet(${lit(probe)})`);
      if (Number(tv.getChildAt(0).get(0)) !== 1) throw new Error('read-back mismatch');
      this.mode = 'opfs';
      try { await this._db.dropFile(probe); } catch { /* fine */ }
      try { await this._dir.removeEntry('_probe.parquet'); } catch { /* fine */ }
    } catch (e) {
      // OPFS is there but DuckDB will not drive it: persist the bytes ourselves.
      this.mode = 'opfs-js';
      this.error = t('DuckDB does not write to OPFS directly ({p1}); the cache is written via a buffer', {p1: String(e && e.message || e).slice(0, 120)});
    }
  }

  get on() { return this.mode && this.mode !== 'off'; }
  fileName(key) { return `${key}.parquet`; }

  // ── storage primitives ────────────────────────────────────────────────
  async _has(key) {
    if (this.mode === 'memory') return this._mem.has(key);
    if (!this._dir) return false;
    try { await this._dir.getFileHandle(this.fileName(key)); return true; } catch { return false; }
  }

  async _readBytes(key) {
    if (this.mode === 'memory') return this._mem.get(key) || null;
    const fh = await this._dir.getFileHandle(this.fileName(key));
    return new Uint8Array(await (await fh.getFile()).arrayBuffer());
  }

  async _writeBytes(key, bytes) {
    if (this.mode === 'memory') { this._mem.set(key, bytes); return; }
    const fh = await this._dir.getFileHandle(this.fileName(key), { create: true });
    const w = await fh.createWritable();
    await w.write(bytes);
    await w.close();
  }

  /** {key: true|false} — which partitions can be served without the file. */
  async probe(keys) {
    await this.ensure();
    const out = {};
    for (const raw of (keys || [])) {
      const k = String(raw);
      // The portable shape (phase 9.2), not v2's own sha1 width: a key an
      // import brought in still names a file this cache can serve.
      out[k] = this.on && PORTABLE_KEY_RE.test(k) && await this._has(k);
    }
    return out;
  }

  /**
   * Write one parsed partition to Parquet, from the SAME Arrow IPC that went
   * into the query instance. Best-effort: a cache that cannot be written only
   * costs the next index its speed, never this one its result.
   */
  async put(key, ipc, partition, file) {
    await this.ensure();
    if (!this.on || !KEY_RE.test(String(key))) return false;
    const rel = `_ep_io_${++this._seq}`;
    try {
      await this._evict();
      await this._conn.insertArrowFromIPCStream(ipc, { name: rel, create: true });
      await this._conn.query(
        `ALTER TABLE ${qi(rel)} ADD COLUMN ${qi(PARTITION_COL)} VARCHAR DEFAULT ${lit(partition ?? '')}`);
      await this._conn.query(
        `ALTER TABLE ${qi(rel)} ADD COLUMN ${qi(FILE_COL)} VARCHAR DEFAULT ${lit(file ?? '')}`);
      if (this.mode === 'opfs') {
        const name = `opfs://${DIR}/${this.fileName(key)}`;
        await this._db.registerOPFSFileName(name);
        await this._conn.query(
          `COPY ${qi(rel)} TO ${lit(name)} (FORMAT PARQUET, COMPRESSION ZSTD)`);
        try { await this._db.dropFile(name); } catch { /* fine */ }
      } else {
        const name = this.fileName(key);
        await this._db.registerEmptyFileBuffer(name);
        await this._conn.query(
          `COPY ${qi(rel)} TO ${lit(name)} (FORMAT PARQUET, COMPRESSION ZSTD)`);
        const buf = await this._db.copyFileToBuffer(name);
        await this._db.dropFile(name);
        await this._writeBytes(key, buf);
      }
      return true;
    } catch (e) {
      this.error = t('Cache not written: {p1}', {p1: String(e && e.message || e).slice(0, 200)});
      return false;
    } finally {
      try { await this._conn.query(`DROP TABLE IF EXISTS ${qi(rel)}`); } catch { /* gone */ }
    }
  }

  /**
   * Read one cached partition back as an Arrow table — `__partition`/`__file`
   * included, because they were written into the Parquet.
   */
  async get(key) {
    await this.ensure();
    if (!this.on || !PORTABLE_KEY_RE.test(String(key)) || !await this._has(key)) {
      throw new Error(t('No partition {p1} in the cache', {p1: key}));
    }
    let name;
    let registered = false;
    if (this.mode === 'opfs') {
      name = `opfs://${DIR}/${this.fileName(key)}`;
      await this._db.registerOPFSFileName(name);
      registered = true;
    } else {
      name = `_in_${this.fileName(key)}`;
      // `.slice()`: registerFileBuffer transfers (detaches) what it is given,
      // and in 'memory' mode that array IS the cache entry.
      await this._db.registerFileBuffer(name, (await this._readBytes(key)).slice());
      registered = true;
    }
    try {
      const tv = await this._conn.query(`SELECT * FROM read_parquet(${lit(name)})`);
      // LRU: touched = recently used. OPFS has no utime, so the write
      // timestamp of a re-written empty marker would be the only equivalent;
      // instead `_evict` falls back to size order for OPFS (see there).
      this._touch(key);
      return tv;
    } finally {
      if (registered) { try { await this._db.dropFile(name); } catch { /* fine */ } }
    }
  }

  _touch(key) {
    if (!this._lru) this._lru = new Map();
    this._lru.delete(key);
    this._lru.set(key, Date.now());
  }

  async stats() {
    await this.ensure();
    if (!this.on) return { files: 0, bytes: 0, mode: this.mode, error: this.error };
    if (this.mode === 'memory') {
      let bytes = 0;
      for (const b of this._mem.values()) bytes += b.byteLength;
      return { files: this._mem.size, bytes, mode: this.mode, error: this.error };
    }
    let files = 0;
    let bytes = 0;
    try {
      for await (const [name, h] of this._dir.entries()) {
        if (!name.endsWith('.parquet')) continue;
        files += 1;
        bytes += (await h.getFile()).size;
      }
    } catch { /* enumeration can fail mid-write; the number is a display */ }
    return { files, bytes, mode: this.mode, error: this.error };
  }

  async clear() {
    await this.ensure();
    let removed = 0;
    if (this.mode === 'memory') {
      removed = this._mem.size;
      this._mem.clear();
      return { removed };
    }
    if (!this._dir) return { removed: 0 };
    const names = [];
    try {
      for await (const [name] of this._dir.entries()) {
        if (name.endsWith('.parquet')) names.push(name);
      }
    } catch { /* nothing to list */ }
    for (const n of names) {
      try { await this._dir.removeEntry(n); removed += 1; } catch { /* held open */ }
    }
    return { removed };
  }

  // ── portable cache (phase 9.2) ────────────────────────────────────────
  // The Parquet a browser wrote is the same file everywhere: a report's run
  // can EXPORT the partitions it indexed and any other user can IMPORT them
  // without ever opening the source workbooks. Keys are validated on the
  // portable shape (`[a-f0-9]{16,64}`) rather than v2's own sha1 width, so an
  // export made by a different key recipe is still storable — what makes it
  // USABLE is the parser version the page checks before offering the manifest.

  /** [{key, bytes}] for the keys this cache actually holds. Missing → omitted. */
  async exportKeys(keys) {
    await this.ensure();
    const out = [];
    if (!this.on) return out;
    for (const raw of (keys || [])) {
      const k = String(raw);
      if (!PORTABLE_KEY_RE.test(k)) continue;
      try {
        if (!await this._has(k)) continue;
        const bytes = await this._readBytes(k);
        if (bytes && bytes.byteLength) out.push({ key: k, bytes });
      } catch { /* a file held open is simply not exported */ }
    }
    return out;
  }

  /**
   * Take one partition in from somebody else's export. Nothing is trusted:
   * the Parquet magic number, then a real `DESCRIBE` through the IO instance,
   * BEFORE the bytes are written under the key the engine reads blindly.
   */
  async importKey(key, bytes) {
    await this.ensure();
    const k = String(key);
    if (!this.on) return { ok: false, error: this.error || t('Partition cache disabled') };
    if (!PORTABLE_KEY_RE.test(k)) return { ok: false, error: t('Bad partition key') };
    const buf = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || new ArrayBuffer(0));
    if (buf.byteLength < 4 || buf[0] !== 0x50 || buf[1] !== 0x41 || buf[2] !== 0x52 || buf[3] !== 0x31) {
      return { ok: false, error: t('This is not Parquet — partition rejected') };
    }
    const probe = `_imp_${++this._seq}_${this.fileName(k)}`;
    try {
      // A COPY: `registerFileBuffer` TRANSFERS the buffer into duckdb's own
      // worker, which detaches ours — validating the bytes would otherwise
      // leave us with an empty array to store under the key.
      await this._db.registerFileBuffer(probe, buf.slice());
      await this._conn.query(`DESCRIBE SELECT * FROM read_parquet(${lit(probe)})`);
    } catch (e) {
      try { await this._db.dropFile(probe); } catch { /* fine */ }
      return { ok: false, error: t('Partition rejected: {p1}', {p1: String(e && e.message || e).slice(0, 200)}) };
    }
    try { await this._db.dropFile(probe); } catch { /* fine */ }
    try {
      await this._evict();
      await this._writeBytes(k, buf);
      this._touch(k);
      return { ok: true, bytes: buf.byteLength };
    } catch (e) {
      return { ok: false, error: t('Partition not written: {p1}', {p1: String(e && e.message || e).slice(0, 200)}) };
    }
  }

  /** 2 GB ceiling, oldest-touched first — the port of `_explore._cache_evict`.
   *  OPFS exposes no mtime, so the order is "what this session touched least
   *  recently, then anything it never touched", which is the same policy with
   *  a worse memory of previous sessions. */
  async _evict(limit = MAX_BYTES) {
    if (!this.on) return;
    const st = await this.stats();
    if (st.bytes <= limit) return;
    if (this.mode === 'memory') {
      for (const k of [...this._mem.keys()]) {
        if ((await this.stats()).bytes <= limit) break;
        this._mem.delete(k);
      }
      return;
    }
    const touched = this._lru || new Map();
    const entries = [];
    try {
      for await (const [name, h] of this._dir.entries()) {
        if (!name.endsWith('.parquet')) continue;
        const key = name.replace(/\.parquet$/, '');
        entries.push({ name, size: (await h.getFile()).size, at: touched.get(key) || 0 });
      }
    } catch { return; }
    entries.sort((a, b) => a.at - b.at);
    let total = entries.reduce((a, e) => a + e.size, 0);
    for (const e of entries) {
      if (total <= limit) break;
      try { await this._dir.removeEntry(e.name); total -= e.size; } catch { /* held open */ }
    }
  }

  async close() {
    try { if (this._conn) await this._conn.close(); } catch { /* gone */ }
    try { if (this._db) await this._db.terminate(); } catch { /* gone */ }
    this._conn = null;
    this._db = null;
  }
}

export { checkTable };
export default ParquetCache;
