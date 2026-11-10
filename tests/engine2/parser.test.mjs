/**
 * engine/xlsx-arrow — the three shapes that broke the Python engine.
 *
 *   node --test tests/engine2/
 *
 * These are behaviour tests, not unit tests: they assert the *contract*
 * `_explore.py` established (normalised names, duplicate suffixes, reserved
 * columns, blanks as NULL, mixed columns as text), because that contract is
 * what /explore's query builder and the semantic layer write SQL against.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { tableFromIPC } from 'apache-arrow';

const ROOT = new URL('../../', import.meta.url);
const fixture = n => readFileSync(fileURLToPath(new URL(`fixtures/${n}`, import.meta.url)));

const parser = await (async () => {
  const mod = await import(new URL('static/xlsx-arrow/xlsx_arrow.js', ROOT).href);
  await mod.default({
    module_or_path: readFileSync(fileURLToPath(new URL('static/xlsx-arrow/xlsx_arrow_bg.wasm', ROOT))),
  });
  return mod;
})();

const parse = (name, sheet = '', header = 0) =>
  tableFromIPC(parser.parse_sheet(fixture(name), sheet, header));

const col = (t, name) => {
  const i = t.schema.fields.findIndex(f => f.name === name);
  assert.notEqual(i, -1, `column ${name} missing; have: ${t.schema.fields.map(f => f.name)}`);
  return { type: String(t.schema.fields[i].type), values: [...t.getChildAt(i)] };
};

test('sheet_names lists the workbook in order', () => {
  // mixed.xlsx keeps the Cyrillic headers of a real 1C export on purpose (see make_fixtures.py).
  assert.deepEqual(Array.from(parser.sheet_names(fixture('mixed.xlsx'))), ['Лист1']);
  assert.deepEqual(Array.from(parser.sheet_names(fixture('part_a.xlsx'))), ['Orders']);
});

test('duplicate headers get __2/__3, blanks get col_N, reserved names step aside', () => {
  const t = parse('mixed.xlsx');
  const names = t.schema.fields.map(f => f.name);
  // "Дата" / "дата" / "  ДАТА  " all normalise to the same key.
  assert.deepEqual(names.slice(0, 5),
    ['дата', 'дата__2', 'дата__3', 'col_4', '__partition__2']);
  // Nothing may collide with the engine's own reserved columns.
  assert.ok(!names.includes('__partition'));
  assert.ok(!names.includes('__file'));
});

test("a numeric column with 1C's ' ' blanks is Float64 with NULLs, not text", () => {
  const c = col(parse('mixed.xlsx'), 'заказ, шт');
  assert.equal(c.type, 'Float64');
  assert.deepEqual(c.values, [10, null, 30.5, null]);
});

test('a column mixing real dates with text and serials becomes Utf8', () => {
  const c = col(parse('mixed.xlsx'), 'тр. дата поставки');
  assert.equal(c.type, 'Utf8');
  assert.deepEqual(c.values, [
    '2026-07-01T10:30:00',   // a real datetime cell, rendered ISO
    '02.07.2026 08:00:00',   // 1C text, untouched
    '46205.5',               // a bare Excel serial in a general-format cell
    null,                    // empty
  ]);
});

test('a pure date column is Timestamp(ms), not text', () => {
  const c = col(parse('mixed.xlsx'), 'дата');
  assert.match(c.type, /Timestamp/);
  assert.equal(c.values.filter(v => v != null).length, 4);
});

test('time-of-day cells are HH:MM:SS text, so partitions can still union', () => {
  const c = col(parse('mixed.xlsx'), 'время');
  assert.equal(c.type, 'Utf8');
  assert.deepEqual(c.values, ['09:15:00', '10:00:00', '23:59:59', null]);
});

test('an all-blank column is Utf8 nulls, never a NULL-typed column', () => {
  const c = col(parse('mixed.xlsx'), 'пусто');
  assert.equal(c.type, 'Utf8');
  assert.deepEqual(c.values, [null, null, null, null]);
});

test('the same column reads numeric in one partition and text in another', () => {
  // Int64, not Float64: every value is whole, which is what `pd.to_numeric`
  // would have produced and what the shipped engine's DuckDB reports as
  // BIGINT. `part_b` has the same column as 1C text.
  assert.equal(col(parse('part_a.xlsx'), 'qty').type, 'Int64');
  assert.equal(col(parse('part_b.xlsx'), 'qty').type, 'Utf8');
});

test('one fractional value makes the whole numeric column Float64', () => {
  // `заказ, шт` holds 10 and 30.5 — integral typing must not silently
  // truncate the 30.5.
  const c = col(parse('mixed.xlsx'), 'заказ, шт');
  assert.equal(c.type, 'Float64');
  assert.deepEqual(c.values, [10, null, 30.5, null]);
});

test('a header_row past the end of the sheet is an error, not a silent empty', () => {
  assert.throws(() => parser.parse_sheet(fixture('part_a.xlsx'), '', 99));
});

test('an unknown sheet name is an error', () => {
  assert.throws(() => parser.parse_sheet(fixture('part_a.xlsx'), 'no such sheet', 0));
});
