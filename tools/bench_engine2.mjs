#!/usr/bin/env node
/**
 * Benchmark the /explore v2 engine over real 1C exports.
 *
 *   node tools/bench_engine2.mjs [--dir ~/data/SL] [--sheet Заказы]
 *                                [--table zakazy] [--json out.json]
 *
 * The files are company data and never enter the repo — point `--dir` at them.
 * Reports, per file: parse ms (Rust calamine -> Arrow IPC), insert ms
 * (duckdb-wasm insertArrowFromIPCStream + the two reserved columns), union
 * rebuild ms. Then three queries chosen to exercise different machinery, and a
 * Parquet write/read round-trip.
 *
 * The baseline to compare against is `tools/bench_engine1.py` (the shipped
 * pandas/DuckDB path). Read that script's header before believing any ratio:
 * CPython is NOT the engine users run, Pyodide is.
 */
import { readFile, writeFile } from 'node:fs/promises';
import { readdirSync } from 'node:fs';
import { createEngine } from '../static/js/explore2/engine.js';

const ROOT = new URL('../', import.meta.url);

function arg(name, dflt) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}

const DIR = arg('dir', `${process.env.HOME}/Downloads/SL`);
const SHEET = arg('sheet', 'Заказы');
const TABLE = arg('table', 'zakazy');
const HEADER = Number(arg('header', '0'));
const JSON_OUT = arg('json', null);

const ms = x => `${x.toFixed(1)}`;

async function main() {
  const files = readdirSync(DIR)
    .filter(f => /\.xlsx?$/i.test(f) && !f.startsWith('~$'))
    .sort();
  if (!files.length) {
    console.error(`no .xlsx in ${DIR} — pass --dir`);
    process.exit(2);
  }

  const t0 = Date.now();
  const eng = await createEngine({ base: ROOT.href + 'static/' });
  const bootMs = Date.now() - t0;
  const st0 = await eng.stats();
  console.log(`engine v2  bundle=${st0.bundle}  parser=${st0.parserVersion}  ` +
    `boot=${bootMs} ms  memory_limit=${st0.memoryLimit}`);
  console.log(`dir=${DIR}  sheet=${SHEET}  table=${TABLE}\n`);

  const out = { boot_ms: bootMs, files: [], queries: [], parquet: {} };

  console.log('file                 bytes    rows   cols  parse   insert  union   MB/s');
  console.log('-------------------------------------------------------------------------');
  for (const f of files) {
    const bytes = await readFile(`${DIR}/${f}`);
    const r = await eng.indexFile(f, bytes, {
      table: TABLE, partition: f.replace(/\.xlsx?$/i, ''), sheet: SHEET, headerRow: HEADER,
    });
    const mb = bytes.length / 1048576;
    console.log(
      `${f.padEnd(20)} ${String(bytes.length).padStart(8)} ${String(r.rows).padStart(6)} ` +
      `${String(r.columns.length).padStart(6)} ${ms(r.ms.parse).padStart(6)} ` +
      `${ms(r.ms.insert).padStart(8)} ${ms(r.ms.union).padStart(6)} ` +
      `${(mb / (r.ms.parse / 1000)).toFixed(1).padStart(6)}`);
    out.files.push({
      file: f, bytes: bytes.length, rows: r.rows, cols: r.columns.length,
      parse_ms: r.ms.parse, insert_ms: r.ms.insert, union_ms: r.ms.union,
      ipc_bytes: r.ipcBytes,
    });
  }

  const totals = out.files.reduce((a, f) => ({
    bytes: a.bytes + f.bytes, rows: a.rows + f.rows,
    parse: a.parse + f.parse_ms, insert: a.insert + f.insert_ms, union: a.union + f.union_ms,
  }), { bytes: 0, rows: 0, parse: 0, insert: 0, union: 0 });
  out.totals = totals;
  console.log('-------------------------------------------------------------------------');
  console.log(`TOTAL                ${String(totals.bytes).padStart(8)} ${String(totals.rows).padStart(6)}` +
    `        ${ms(totals.parse).padStart(6)} ${ms(totals.insert).padStart(8)} ${ms(totals.union).padStart(6)}`);
  console.log(`index wall-clock: ${ms(totals.parse + totals.insert + totals.union)} ms ` +
    `for ${totals.rows} rows / ${(totals.bytes / 1048576).toFixed(1)} MB\n`);

  // Column names for the three queries are discovered, not hardcoded: these
  // are real exports and their headers move.
  const cols = await eng.raw(`DESCRIBE SELECT * FROM ${TABLE}`);
  const schema = cols.toArray().map(r => r.toJSON())
    .map(r => ({ name: String(r.column_name), type: String(r.column_type) }));
  const numeric = schema.find(c => /DOUBLE|BIGINT|INTEGER|DECIMAL/.test(c.type)
    && !c.name.startsWith('__'));
  const text = schema.find(c => c.type === 'VARCHAR' && !c.name.startsWith('__'));
  const date = schema.find(c => /TIMESTAMP|DATE/.test(c.type));

  const q = (label, sql) => ({ label, sql });
  const queries = [
    q('count per __partition',
      `SELECT __partition, count(*) AS n FROM ${TABLE} GROUP BY 1 ORDER BY 1`),
  ];
  if (numeric && text) {
    queries.push(q(`SUM(${numeric.name}) by ${text.name}`,
      `SELECT "${text.name}" AS k, sum("${numeric.name}") AS s FROM ${TABLE} ` +
      `GROUP BY 1 ORDER BY s DESC NULLS LAST LIMIT 20`));
  }
  if (date) {
    queries.push(q(`filter on ${date.name}`,
      `SELECT count(*) AS n FROM ${TABLE} WHERE "${date.name}" >= TIMESTAMP '2000-01-01'`));
  } else {
    // Every date column in this export read as mixed text (1C writes real
    // dates, 'dd.mm.yyyy hh:mm:ss' strings and Excel serials into one column)
    // — which is itself the finding. Filter on the text instead.
    const anyDate = schema.find(c => /дата|date/i.test(c.name));
    if (anyDate) {
      queries.push(q(`filter on ${anyDate.name} (VARCHAR — see note)`,
        `SELECT count(*) AS n FROM ${TABLE} WHERE "${anyDate.name}" LIKE '2026%'`));
    }
  }

  console.log('query                                              rows      ms   (best of 3)');
  console.log('-------------------------------------------------------------------------');
  for (const { label, sql } of queries) {
    let best = Infinity, rows = 0;
    for (let i = 0; i < 3; i++) {
      const r = await eng.query(sql, 1000);
      best = Math.min(best, r.ms);
      rows = r.total;
    }
    console.log(`${label.padEnd(50)} ${String(rows).padStart(5)} ${ms(best).padStart(7)}`);
    out.queries.push({ label, sql, rows, ms: best });
  }
  console.log();

  // Parquet: the partition cache. `opfs:false` in Node — there is no OPFS
  // here, and the honest browser number needs a browser (docs/ENGINE2.md).
  const w = await eng.toParquet(TABLE, { opfs: false });
  await eng.reset();
  const rd = await eng.fromParquet(TABLE, { bytes: w.bytes });
  const check = await eng.query(`SELECT count(*) FROM ${TABLE}`);
  out.parquet = {
    write_ms: w.ms, read_ms: rd.ms, bytes: w.bytes.length,
    rows: check.rows[0][0], opfs: false,
  };
  console.log(`parquet write ${ms(w.ms)} ms -> ${(w.bytes.length / 1048576).toFixed(1)} MB ` +
    `(${(totals.bytes / w.bytes.length).toFixed(1)}x smaller than the xlsx)`);
  console.log(`parquet read  ${ms(rd.ms)} ms -> ${check.rows[0][0]} rows back\n`);

  const st = await eng.stats();
  console.log(`duckdb memory in use: ${st.memoryUsage} of ${st.memoryLimit}`);
  console.log(`node rss: ${(process.memoryUsage().rss / 1048576).toFixed(0)} MB`);
  out.stats = st;
  out.rss_mb = process.memoryUsage().rss / 1048576;

  if (JSON_OUT) {
    await writeFile(JSON_OUT, JSON.stringify(out, null, 2));
    console.log(`\nwrote ${JSON_OUT}`);
  }
  await eng.close();
  process.exit(0);
}

main().catch(e => { console.error(e); process.exit(1); });
