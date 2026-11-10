/**
 * static/js/explore2/engine.js — index two partitions, union them, query.
 *
 *   node --test tests/engine2/
 *
 * The interesting assertion is the type conflict: `part_a.qty` is numeric and
 * `part_b.qty` is 1C text with a `' '` blank. Left to DuckDB the union casts
 * the text to the number and dies; `rebuildView` widens the column to VARCHAR
 * on purpose. That behaviour is the reason `_rebuild_view` exists, and it is
 * the one thing a rewrite is most likely to lose.
 */
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createEngine, guardSql, checkTable, SQLGuardError, ExploreError }
  from '../../static/js/explore2/engine.js';

const ROOT = new URL('../../', import.meta.url);
const fixture = n => readFileSync(fileURLToPath(new URL(`fixtures/${n}`, import.meta.url)));

let eng;
before(async () => {
  eng = await createEngine({ base: ROOT.href + 'static/' });
  await eng.indexFile('part_a.xlsx', fixture('part_a.xlsx'),
    { table: 'orders_raw', partition: 'a', sheet: 'Orders', headerRow: 0 });
  await eng.indexFile('part_b.xlsx', fixture('part_b.xlsx'),
    { table: 'orders_raw', partition: 'b', sheet: 'Orders', headerRow: 0 });
}, { timeout: 120000 });

after(async () => { if (eng) await eng.close(); });

test('two partitions land in one view with __partition/__file', async () => {
  const r = await eng.query('SELECT __partition, count(*) n FROM orders_raw GROUP BY 1 ORDER BY 1');
  assert.deepEqual(r.rows, [['a', 5], ['b', 6]]);
  const f = await eng.query('SELECT DISTINCT __file FROM orders_raw ORDER BY 1');
  assert.deepEqual(f.rows, [['part_a.xlsx'], ['part_b.xlsx']]);
});

test('a numeric/text type conflict widens to VARCHAR instead of failing', async () => {
  const d = await eng.raw('DESCRIBE SELECT * FROM orders_raw');
  const types = Object.fromEntries(
    d.toArray().map(r => [r.toJSON().column_name, String(r.toJSON().column_type)]));
  assert.equal(types['qty'], 'VARCHAR');
  // …and the `' '` blank survived as NULL rather than exploding an INT32 guess.
  const n = await eng.query("SELECT count(*) FROM orders_raw WHERE qty IS NULL");
  assert.equal(n.rows[0][0], 1);
});

test('a column with the same type everywhere keeps that type', async () => {
  const d = await eng.raw('DESCRIBE SELECT * FROM orders_raw');
  const types = Object.fromEntries(
    d.toArray().map(r => [r.toJSON().column_name, String(r.toJSON().column_type)]));
  assert.match(types['date'], /TIMESTAMP/);
  assert.equal(types['client'], 'VARCHAR');
});

test('re-indexing the same file replaces its partition, never doubles it', async () => {
  await eng.indexFile('part_a.xlsx', fixture('part_a.xlsx'),
    { table: 'orders_raw', partition: 'a', sheet: 'Orders', headerRow: 0 });
  const r = await eng.query('SELECT count(*) FROM orders_raw');
  assert.equal(r.rows[0][0], 11);
  assert.equal(eng.tables()[0].partitions.length, 2);
});

test('a grouped aggregate over the union answers across partitions', async () => {
  const r = await eng.query(
    'SELECT client, sum(TRY_CAST(qty AS DOUBLE)) s FROM orders_raw GROUP BY 1 ORDER BY 1 LIMIT 3');
  assert.equal(r.columns[0], 'client');
  assert.equal(r.rows.length, 3);
  assert.equal(r.rows[0][1], 0);   // "client 0" in both partitions: 0 + 0
});

test('Parquet round-trip through DuckDB reproduces the table', async () => {
  const w = await eng.toParquet('orders_raw', { opfs: false });
  assert.ok(w.bytes instanceof Uint8Array && w.bytes.length > 0);
  await eng.reset();
  assert.equal(eng.tables().length, 0);
  await eng.fromParquet('orders_raw', { bytes: w.bytes });
  const r = await eng.query('SELECT __partition, count(*) n FROM orders_raw GROUP BY 1 ORDER BY 1');
  // One Parquet file is one relation, so the partition COLUMN still separates
  // the rows even though the union now has a single member.
  assert.deepEqual(r.rows, [['a', 5], ['b', 6]]);
});

test('the SQL guard rejects everything that is not one SELECT', () => {
  assert.equal(guardSql('  SELECT 1  '), 'SELECT 1');
  assert.equal(guardSql('WITH x AS (SELECT 1) SELECT * FROM x').slice(0, 4), 'WITH');
  for (const bad of [
    'SELECT 1; DROP TABLE orders_raw',
    'SELECT 1 -- \n; DELETE FROM orders_raw',
    'DROP TABLE orders_raw',
    "SELECT * FROM read_parquet('/etc/passwd')",
    'ATTACH \'x.db\'',
    'SELECT 1; SELECT 2',
    '',
  ]) {
    assert.throws(() => guardSql(bad), SQLGuardError, `should reject: ${bad}`);
  }
  // A literal that merely CONTAINS a forbidden word is fine — masking first is
  // the whole point.
  assert.doesNotThrow(() => guardSql("SELECT 'drop the base' AS x"));
});

test('table names are validated, not trusted', () => {
  // Cyrillic on purpose: a non-ASCII table name is legal and must pass.
  assert.equal(checkTable('заказы_2026'), 'заказы_2026');
  for (const bad of ['a"b', 'a;b', 'a b', '', null, '_ep_1']) {
    assert.throws(() => checkTable(bad), ExploreError, `should reject: ${bad}`);
  }
});
