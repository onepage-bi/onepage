// Pure-function unit test for the worker's per-file check/declared-column
// filters (P1.2). No npm deps — plain node assert, run with:
//   node tests/checks_filter.js
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub

const assert = require('assert');
const path = require('path');
const { checksFor, declaredFor } = require(path.join(__dirname, '..', 'static', 'js', 'gen-worker.js'));

let n = 0;
function check(name, fn) {
  try {
    fn();
    n++;
    console.log('ok - ' + name);
  } catch (e) {
    console.error('FAIL - ' + name);
    console.error(e);
    process.exitCode = 1;
  }
}

// ── checksFor ─────────────────────────────────────────────────────────
check('checksFor scopes to the matching file only', () => {
  const checks = [
    { id: 'not_null:*.xlsx:*:SKU', match: '*.xlsx', sheet: '*', column: 'SKU', type: 'not_null' },
    { id: 'range:report_*.xlsx:*:Qty', match: 'report_*.xlsx', sheet: '*', column: 'Qty', type: 'range' },
  ];
  const out = checksFor(checks, '*.xlsx');
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].id, 'not_null:*.xlsx:*:SKU');
});

check('checksFor returns [] for an unmatched file (no matched_input)', () => {
  const checks = [{ id: 'a', match: '*.xlsx' }];
  assert.deepStrictEqual(checksFor(checks, undefined), []);
  assert.deepStrictEqual(checksFor(checks, null), []);
});

check('checksFor handles missing/empty checks array', () => {
  assert.deepStrictEqual(checksFor(undefined, '*.xlsx'), []);
  assert.deepStrictEqual(checksFor([], '*.xlsx'), []);
});

check('checksFor tolerates junk entries without throwing', () => {
  const checks = [null, undefined, 42, { match: '*.xlsx' }];
  const out = checksFor(checks, '*.xlsx');
  assert.strictEqual(out.length, 1);
});

// ── declaredFor ──────────────────────────────────────────────────────
check('declaredFor reshapes sheets into {sheet_glob: [columns]}', () => {
  const inputs = [
    {
      match: 'report_*.xlsx',
      sheets: [
        { name: 'Data', columns: ['SKU', 'Qty'] },
        { name: 'Ref*', columns: ['Code'] },
      ],
    },
    { match: 'other_*.xlsx', sheets: [{ name: '*', columns: ['X'] }] },
  ];
  const out = declaredFor(inputs, 'report_*.xlsx');
  assert.deepStrictEqual(out, { Data: ['SKU', 'Qty'], 'Ref*': ['Code'] });
});

check('declaredFor skips sheets with no columns', () => {
  const inputs = [{ match: 'a', sheets: [{ name: 'Empty', columns: [] }, { name: 'Full', columns: ['C'] }] }];
  const out = declaredFor(inputs, 'a');
  assert.deepStrictEqual(out, { Full: ['C'] });
});

check('declaredFor returns {} for no matched_input, no match, or no inputs', () => {
  assert.deepStrictEqual(declaredFor([{ match: 'a', sheets: [] }], undefined), {});
  assert.deepStrictEqual(declaredFor([{ match: 'a', sheets: [] }], 'b'), {});
  assert.deepStrictEqual(declaredFor(undefined, 'a'), {});
  assert.deepStrictEqual(declaredFor([], 'a'), {});
});

check('declaredFor defaults an unnamed sheet to "*"', () => {
  const inputs = [{ match: 'a', sheets: [{ columns: ['C'] }] }];
  assert.deepStrictEqual(declaredFor(inputs, 'a'), { '*': ['C'] });
});

console.log(n + ' passed');
if (process.exitCode) process.exit(process.exitCode);
