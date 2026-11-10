/* Unit test for gen-engine.js's proactive observation-body shrink (P4.1 fix):
 * the final observe batch must fit the server's _MAX_OBS_BODY cap BEFORE it
 * is ever sent, because a 413 on the final batch drops `final:true` outright
 * (obsFinalSent forbids a retry) and observations.finalize() never runs.
 * No npm deps — plain node assert, run with:
 *   node tests/gen_engine_observe.js
 */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub

const assert = require('assert');
const path = require('path');
const { shrinkObservationsBody, _stripSchema, _stripStage } =
  require(path.join(__dirname, '..', 'static', 'js', 'gen-engine.js'));

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

function bigStage(i, cols) {
  const profiles = [];
  for (let c = 0; c < cols; c++) {
    profiles.push({ sheet: null, column: 'c' + c, type: 'number', nulls: 0, count: 1000,
                    distinct: 5, min: 0, max: 9, top: [['1', 5], ['2', 4]], sampled: false });
  }
  return { name: 's' + i, order: i, rows: 1000, columns: profiles.map(p => p.column),
           profiles: profiles, inputs: null, read_files: [], ms: 1.0 };
}

function bigFile(i) {
  return {
    name: 'f' + i + '.xlsx', dir: 0, kind: 'parsed',
    schema: { sheets: [{ name: 'Sheet1', header: Array.from({ length: 50 }, (_, k) => 'col' + k) }] },
    checks: [{ check_id: 'c', status: 'pass' }],
    profiles: [{ sheet: 'Sheet1', column: 'col0', type: 'text', nulls: 0, count: 1,
                distinct: 1, min: 'a', max: 'a', top: [['a', 1]], sampled: false }],
  };
}

// ── _stripSchema / _stripStage ──────────────────────────────────────────
check('_stripSchema drops schema/checks/profiles, keeps the rest', () => {
  const f = bigFile(0);
  const s = _stripSchema(f);
  assert.strictEqual(s.name, f.name);
  // an observed file keeps an EXPLICIT empty schema with a reason — absent
  // `schema` means "unchanged" to the server and would hide the gap
  assert.deepStrictEqual(s.schema, { sheets: [], error: 'stripped to fit the observation batch cap' });
  assert.strictEqual(s.checks, undefined);
  assert.strictEqual(s.profiles, undefined);
  assert.ok('schema' in JSON.parse(JSON.stringify(s)));
});

check('_stripSchema keeps an unchanged file (schema undefined) unchanged', () => {
  const f = Object.assign({}, bigFile(0), { schema: undefined });
  const s = _stripSchema(f);
  assert.strictEqual(s.schema, undefined);
  assert.ok(!('schema' in JSON.parse(JSON.stringify(s))));
});

check('_stripStage drops only profiles', () => {
  const st = bigStage(0, 3);
  const s = _stripStage(st);
  assert.strictEqual(s.name, st.name);
  assert.deepStrictEqual(s.columns, st.columns);
  assert.strictEqual(s.profiles, undefined);
});

// ── shrinkObservationsBody ───────────────────────────────────────────────
check('fits as-is: payload/body pass through unchanged, not marked stripped', () => {
  const files = [bigFile(0)];
  const payload = { seq: 1, files: files, final: true };
  const r = shrinkObservationsBody(payload, files, null, false, 2 * 1024 * 1024);
  assert.strictEqual(r.stripped, false);
  assert.strictEqual(r.payload, payload);
  assert.deepStrictEqual(JSON.parse(r.body), payload);
});

check('oversized: strips schema/checks/profiles from files and stages, fits after', () => {
  const files = Array.from({ length: 5 }, (_, i) => bigFile(i));
  const stages = [bigStage(0, 64)];
  const payload = { seq: 1, files: files, final: true, stages: stages };
  const cap = 3000;   // smaller than the raw payload, big enough after stripping
  const r = shrinkObservationsBody(payload, files, stages, false, cap);
  assert.strictEqual(r.stripped, true);
  assert.ok(new Blob([r.body]).size <= cap, 'stripped body still exceeds cap: ' + r.body.length);
  const parsed = JSON.parse(r.body);
  assert.ok(parsed.files.every(f => f.schema && f.schema.error && f.schema.sheets.length === 0 && !('checks' in f) && !('profiles' in f)));
  assert.ok(parsed.stages.every(s => !('profiles' in s)));
  assert.strictEqual(parsed.final, true, 'final:true must survive shrinking');
});

check('still oversized after stripping: stages dropped outright, files/final survive', () => {
  const files = Array.from({ length: 2 }, (_, i) => bigFile(i));
  const stages = Array.from({ length: 32 }, (_, i) => bigStage(i, 64));
  const payload = { seq: 1, files: files, final: true, stages: stages };
  const cap = 500;   // too small even for the stripped files alone with 32 stage names
  const r = shrinkObservationsBody(payload, files, stages, false, cap);
  const parsed = JSON.parse(r.body);
  assert.ok(!('stages' in parsed), 'stages must be the thing dropped, never `final`');
  assert.strictEqual(parsed.final, true);
  assert.ok(Array.isArray(parsed.files));
});

check('already stripped (obsStripSchema carried in): skips the strip step, still fits', () => {
  const files = [bigFile(0)];
  const stages = [bigStage(0, 64)];
  const payload = { seq: 2, files: files.map(_stripSchema), final: true, stages: stages.map(_stripStage) };
  const r = shrinkObservationsBody(payload, files, stages, true, 2 * 1024 * 1024);
  assert.strictEqual(r.stripped, true);
  assert.strictEqual(r.payload, payload, 'no re-strip work when already stripped and it already fits');
});

check('no stages at all: shrink never invents a stages key', () => {
  const files = [bigFile(0)];
  const payload = { seq: 1, files: files, final: true };
  const cap = 50;   // forces both strip attempts
  const r = shrinkObservationsBody(payload, files, null, false, cap);
  assert.ok(!('stages' in JSON.parse(r.body)));
});

console.log('\n' + n + ' checks passed');
