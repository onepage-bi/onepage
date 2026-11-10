/**
 * static/js/explore2/semantics.js — ADR-13 in engine v2.
 *
 * The same ground `tests/smoke_explore_py.py::test_semantics` covers for the
 * Pyodide engine, against a real duckdb-wasm here: bindings become `sem` views
 * over the indexed catalogue partitions, an inline `rows` table materialises
 * typed, a declared view is created in order, readiness is computed over the
 * DECLARED names, and an intent becomes deterministic SQL the guard accepts
 * and the engine executes.
 *
 * The fixture is the committed `part_a`/`part_b` pair (Client / qty / Date),
 * with `qty` numeric in one partition and 1C text in the other — so the
 * semantic `number` cast is exercised over a VARCHAR union, which is the whole
 * reason `_sem_cast` exists.
 */
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createEngine } from '../../static/js/explore2/engine.js';
import { SemanticLayer } from '../../static/js/explore2/semantics.js';
import { guardSql } from '../../static/js/explore2/guard.js';

const ROOT = new URL('../../', import.meta.url);
const fixture = n => readFileSync(fileURLToPath(new URL(`fixtures/${n}`, import.meta.url)));

const SEM = {
  schema_version: 1,
  generator: 'semtest',
  tables: {
    orders: {
      description: 'Orders',
      sources: [{ file: 'part_*.xlsx', sheet: 'Orders' }],
      columns: {
        client: { source: 'Client', type: 'text', role: 'key' },
        amount: { source: 'qty', type: 'number', role: 'measure' },
        day: { source: 'Date', type: 'date', role: 'time' },
        missing: { source: 'No such column', type: 'text', role: 'dim' },
      },
    },
    tiers: {
      description: 'inline',
      sources: [{ object: 'cfg' }],
      columns: { client: { type: 'text', role: 'key' }, tier: { type: 'text', role: 'attr' } },
      rows: [{ client: 'client 0', tier: 'gold' }, { client: 'client 1', tier: 'silver' }],
    },
  },
  views: {
    lines: {
      sql: "SELECT o.*, coalesce(t.tier, 'none') AS tier FROM orders o "
        + 'LEFT JOIN tiers t USING (client)',
    },
  },
  kpis: {
    avg_amount: {
      name: 'Average order', short: 'Average order', definition: 'Σ amount / number of orders',
      aggregation: 'ratio', measure: 'ratio', dims: ['tier', 'day'],
      cell_sql: 'SELECT tier, day, SUM(amount) AS n, COUNT(*) AS d FROM lines GROUP BY 1, 2',
    },
    orphan: {
      name: 'Metric over an unbound table', short: 'Orphan', dims: [],
      cell_sql: 'SELECT SUM(amount) AS value FROM nowhere',
    },
  },
};

const BINDINGS = {
  orders: {
    inline: false,
    matches: [{
      table: 'orders_raw', table_id: 't_z',
      files: ['part_a.xlsx', 'part_b.xlsx'],
      columns: { client: 'client', amount: 'qty', day: 'date', missing: null },
    }],
  },
  tiers: { inline: true, matches: [] },
};

let eng;
let sem;
const ctx = () => eng.semCtx();
const rows = async (sql) => (await eng.query(guardSql(sql))).rows;

before(async () => {
  eng = await createEngine({ base: ROOT.href + 'static/' });
  await eng.indexFile('part_a.xlsx', fixture('part_a.xlsx'),
    { table: 'orders_raw', partition: 'a', sheet: 'Orders', headerRow: 0 });
  await eng.indexFile('part_b.xlsx', fixture('part_b.xlsx'),
    { table: 'orders_raw', partition: 'b', sheet: 'Orders', headerRow: 0 });
  sem = new SemanticLayer();
  sem.setSemantics([{ generator: 'semtest', generator_id: 7, semantics: SEM, bindings: BINDINGS }]);
}, { timeout: 120000 });

after(async () => { if (eng) await eng.close(); });

test('bindings become sem views, inline rows materialise, declared views follow', async () => {
  const st = await sem.status(ctx());
  const g = st.generators[0];
  assert.equal(g.tables.orders, 'bound', JSON.stringify(g));
  assert.equal(g.tables.tiers, 'inline', JSON.stringify(g));
  assert.equal(g.views.lines, 'ok', JSON.stringify(g));
  const kpi = g.kpis.find(k => k.key === 'avg_amount');
  assert.equal(kpi.ready, true, JSON.stringify(g.kpis));
  // Ready is computed over the DECLARED names: a KPI whose SQL names a table
  // that does not exist at all is not ready, however well the rest bound.
  assert.equal(g.kpis.find(k => k.key === 'orphan').ready, false);
});

test('the inline table is typed like the server dry run, engine columns present', async () => {
  assert.deepEqual(await rows('SELECT tier, __file FROM tiers ORDER BY tier'),
    [['gold', null], ['silver', null]]);
});

test('a missing source column is NULL, not an error', async () => {
  const r = await rows('SELECT count(*) FROM orders WHERE missing IS NULL');
  assert.equal(r[0][0], 11);
});

test('the number cast parses 1C text over a VARCHAR union', async () => {
  // part_b's qty is text with a ' ' blank; the union widened it to VARCHAR and
  // `semCast('number')` re-parses it — the blank becomes NULL, not 0.
  const r = await rows('SELECT count(amount), sum(amount) FROM orders');
  assert.equal(r[0][0], 10);
  assert.equal(r[0][1], 200);
});

test('the catalogue table stays readable — search_path keeps main', async () => {
  const r = await rows('SELECT count(*) FROM orders_raw');
  assert.equal(r[0][0], 11);
});

test('the declared view is readable through the ordinary guarded SQL path', async () => {
  const r = await rows('SELECT tier, count(*) AS n FROM lines GROUP BY 1 ORDER BY 1');
  assert.deepEqual(r.map(x => x[0]), ['gold', 'none', 'silver']);
});

test('an intent becomes SQL the guard accepts and the engine runs', async () => {
  const out = await sem.intentSql({
    kpi: 'avg_amount', generator_id: 7, dims: ['tier'],
    order: { by: 'value', dir: 'desc' }, limit: 10,
  }, ctx());
  assert.equal(out.error, null, JSON.stringify(out));
  assert.ok(out.sql.startsWith('WITH c AS'));
  assert.doesNotThrow(() => guardSql(out.sql));
  const res = await eng.query(out.sql);
  assert.equal(res.columns[0], 'tier');
  assert.equal(res.columns.at(-1), 'value');
  assert.equal(res.rows.length, 3);
});

test('an intent refuses everything it cannot ground', async () => {
  const bad = async (intent) => (await sem.intentSql(intent, ctx())).error;
  assert.ok(await bad({ kpi: 'avg_amount', generator_id: 7, dims: ['nope'] }));
  assert.ok(await bad({ kpi: 'zzz', generator_id: 7 }));
  assert.ok(await bad({ kpi: 'avg_amount' }));                       // no generator_id
  assert.ok(await bad({ kpi: 'orphan', generator_id: 7 }));          // not ready
  assert.ok(await bad({ kpi: 'avg_amount', generator_id: 7, filters: [{ dim: 'tier', op: 'drop' }] }));
  assert.ok(await bad({ kpi: 'avg_amount', generator_id: 7, filters: [{ dim: 'tier', op: '=' }] }));
  assert.ok(await bad({ kpi: 'avg_amount', generator_id: 7, order: { by: 'nope' } }));
});

test('a filter value is a quoted literal, never interpolated', async () => {
  const out = await sem.intentSql({
    kpi: 'avg_amount', generator_id: 7,
    filters: [{ dim: 'tier', op: '=', value: "x' OR 1=1" }],
  }, ctx());
  assert.ok(out.sql.includes("'x'' OR 1=1'"), out.sql);
  assert.doesNotThrow(() => guardSql(out.sql));
});

test('the ratio measure rolls up as n/d, a plain one by its declared aggregation', async () => {
  const ratio = await sem.intentSql({ kpi: 'avg_amount', generator_id: 7 }, ctx());
  assert.ok(ratio.sql.includes('SUM("n") AS "n"') && ratio.sql.includes('NULLIF(SUM("d"), 0)'));
  const avg = { ...SEM, kpis: { one: { ...SEM.kpis.avg_amount, measure: 'value',
    aggregation: 'avg', cell_sql: 'SELECT tier, AVG(amount) AS value FROM lines GROUP BY 1' } } };
  const s2 = new SemanticLayer();
  s2.setSemantics([{ generator: 'g', generator_id: 9, semantics: avg, bindings: BINDINGS }]);
  const out = await s2.intentSql({ kpi: 'one', generator_id: 9 }, ctx());
  assert.ok(out.sql.includes('AVG("value") AS "value"'), out.sql);
});

test('only the files the source pattern admitted feed a bound table', async () => {
  const narrow = { ...BINDINGS, orders: { inline: false,
    matches: [{ ...BINDINGS.orders.matches[0], files: ['part_a.xlsx'] }] } };
  sem.setSemantics([{ generator: 'semtest', generator_id: 7, semantics: SEM, bindings: narrow }]);
  await sem.status(ctx());
  assert.equal((await rows('SELECT count(*) FROM orders'))[0][0], 5);
});

test('an unbound table makes its KPI not ready and refuses the intent', async () => {
  const unbound = { ...BINDINGS, orders: { inline: false, matches: [] } };
  sem.setSemantics([{ generator: 'semtest', generator_id: 7, semantics: SEM, bindings: unbound }]);
  const g = (await sem.status(ctx())).generators[0];
  assert.equal(g.tables.orders, 'unbound');
  assert.equal(g.kpis.find(k => k.key === 'avg_amount').ready, false);
  const out = await sem.intentSql({ kpi: 'avg_amount', generator_id: 7 }, ctx());
  assert.match(out.error, /not ready/);
});

test('a re-index marks the layer dirty and it rebuilds transparently', async () => {
  sem.setSemantics([{ generator: 'semtest', generator_id: 7, semantics: SEM, bindings: BINDINGS }]);
  await sem.status(ctx());
  assert.equal((await rows('SELECT count(*) FROM orders'))[0][0], 11);
  await eng.indexFile('part_a.xlsx', fixture('part_a.xlsx'),
    { table: 'orders_raw', partition: 'a', sheet: 'Orders', headerRow: 0 });
  sem.markDirty();
  await sem.status(ctx());
  assert.equal((await rows('SELECT count(*) FROM orders'))[0][0], 11);
});

test('a semantic name resolves to the catalogue tables it stands on', async () => {
  await sem.status(ctx());
  assert.deepEqual([...sem.depsFor('orders')], ['orders_raw']);
  assert.deepEqual([...sem.depsFor('lines')], ['orders_raw']);   // through the view
});
