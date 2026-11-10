/**
 * The self-hosted DuckDB extension mirror actually works.
 *
 * Why this test exists: `parquet` is NOT statically linked into
 * duckdb-eh.wasm 1.32.0 (`duckdb_extensions()` says NOT_INSTALLED at boot), so
 * the first `COPY … (FORMAT PARQUET)` auto-installs it from
 * extensions.duckdb.org. On a machine with internet that is invisible; on the
 * corporate network it is a broken partition cache and no obvious cause.
 *
 * So: serve `static/duckdb-ext/` from a throwaway localhost server, point
 * `custom_extension_repository` at it, and assert that INSTALL/LOAD and a real
 * Parquet round-trip go through it. The server logs every request, and the
 * test asserts the extension byte-range requests landed on OUR server — which
 * is the only way to know the CDN was not silently used instead.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, stat, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createEngine } from '../../static/js/explore2/engine.js';

const ROOT = new URL('../../', import.meta.url);
const fixture = n => readFile(fileURLToPath(new URL(`fixtures/${n}`, import.meta.url)));

test('parquet installs from static/duckdb-ext/, not from the CDN', async (t) => {
  const hits = [];
  const server = createServer(async (req, res) => {
    hits.push(req.url);
    try {
      const p = fileURLToPath(new URL('static' + req.url.split('?')[0], ROOT));
      const body = await readFile(p);
      const s = await stat(p);
      res.writeHead(200, {
        'content-type': 'application/wasm',
        'content-length': String(s.size),
        // duckdb's extension loader issues range requests when it can.
        'accept-ranges': 'bytes',
      });
      res.end(body);
    } catch {
      res.writeHead(404).end('nope');
    }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  t.after(() => new Promise(r => server.close(r)));

  // In Node — and ONLY in Node — duckdb-wasm's NODE_FS runtime caches a
  // loaded extension under `$HOME/.duckdb/extensions/`, so a second run would
  // never touch the network and this test would pass for the wrong reason.
  // A throwaway HOME (inherited by the worker thread) makes each run cold.
  // A browser has no such cache: every tab loads the extension afresh, which
  // is exactly why the mirror has to exist.
  const home = process.env.HOME;
  process.env.HOME = await mkdtemp(join(tmpdir(), 'op-explore2-home-'));
  const fakeHome = process.env.HOME;
  t.after(async () => {
    process.env.HOME = home;
    await rm(fakeHome, { recursive: true, force: true });
  });

  const eng = await createEngine({
    // The wasm binaries still load from disk (Node cannot fetch file:), but
    // the extension repository is the thing under test and it is HTTP.
    base: ROOT.href + 'static/',
    extensionRepository: `http://127.0.0.1:${port}/duckdb-ext/`,
  });
  t.after(() => eng.close());

  await eng.indexFile('part_a.xlsx', await fixture('part_a.xlsx'),
    { table: 't', partition: 'a', sheet: 'Orders', headerRow: 0 });

  const w = await eng.toParquet('t', { opfs: false });
  assert.ok(w.bytes.length > 0, 'COPY TO parquet produced bytes');

  assert.ok(
    hits.some(u => u.includes('parquet.duckdb_extension.wasm')),
    `the extension was never requested from us; hits: ${JSON.stringify(hits)}`);

  const ext = await eng.raw(
    "SELECT loaded, install_mode FROM duckdb_extensions() WHERE extension_name='parquet'");
  assert.equal(ext.toArray()[0].toJSON().loaded, true);
}, { timeout: 180000 });
