/* Unit test for the worker's stage plumbing (P4.1): the 32-stage ceiling and
 * the shape of the 'observe' message that carries the run's stages.
 * No npm deps — plain node assert, run with:
 *   node tests/stages_flush.js
 */
'use strict';
require('./_i18n_stub');   // global t/tn — must come before the DOM stub

const assert = require('assert');
const path = require('path');
const { takeStages, takeStagesBudgeted, observePayload, byteLength } = require(path.join(__dirname, '..', 'static', 'js', 'gen-worker.js'));

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

function stage(i) {
  return { name: 's' + i, order: i, rows: 1, columns: ['a'], profiles: [], inputs: null, read_files: [], ms: 0.1 };
}

// ── takeStages ────────────────────────────────────────────────────────
check('takeStages appends in order', () => {
  const out = takeStages([], [stage(0), stage(1)], 32);
  assert.deepStrictEqual(out.map(s => s.name), ['s0', 's1']);
});

check('takeStages stops at the ceiling', () => {
  const many = [];
  for (let i = 0; i < 40; i++) many.push(stage(i));
  const out = takeStages([], many, 32);
  assert.strictEqual(out.length, 32);
  assert.strictEqual(out[31].order, 31);
});

check('takeStages counts what is already there', () => {
  const out = takeStages([stage(0), stage(1)], [stage(2), stage(3)], 3);
  assert.deepStrictEqual(out.map(s => s.name), ['s0', 's1', 's2']);
});

check('takeStages tolerates junk from a stubbed probe', () => {
  assert.deepStrictEqual(takeStages([], null, 32), []);
  assert.deepStrictEqual(takeStages([], undefined, 32), []);
  assert.deepStrictEqual(takeStages([], [null, 'x', 7], 32), []);
});

// ── takeStagesBudgeted (P4.1 fix: stages must charge against the same byte
// budget files already spend from — observeBytes/OBSERVE_BUDGET_BYTES — or
// the final observe batch can blow the server's body cap with no accounting
// at all and get dropped whole, `final:true` included) ───────────────────

function heavyStage(i) {
  const profiles = [];
  for (let c = 0; c < 64; c++) {
    profiles.push({ sheet: null, column: 'col' + c, type: 'number', nulls: 0,
                    count: 1000, distinct: 5, min: 0, max: 9,
                    top: [['1', 5], ['2', 4], ['3', 3], ['4', 2], ['5', 1]], sampled: false });
  }
  return { name: 'heavy' + i, order: i, rows: 200000, columns: profiles.map(p => p.column),
           profiles: profiles, inputs: null, read_files: [], ms: 4000.0 };
}

check('takeStagesBudgeted: fits under budget -> all appended, full weight charged', () => {
  const out = [];
  const one = heavyStage(0);
  const oneSize = JSON.stringify(one).length;
  const { spent, truncated } = takeStagesBudgeted(out, [heavyStage(0)], 32, oneSize + 10);
  assert.strictEqual(out.length, 1);
  assert.strictEqual(spent, oneSize);
  assert.strictEqual(truncated, false);
});

check('takeStagesBudgeted: no budget left -> stage stripped of profiles, or dropped', () => {
  const out = [];
  const { spent, truncated } = takeStagesBudgeted(out, [heavyStage(0)], 32, 0);
  assert.strictEqual(out.length, 0, 'zero budget cannot fit even a stripped stage');
  assert.strictEqual(spent, 0);
  assert.strictEqual(truncated, true);
});

check('takeStagesBudgeted: fits stripped (profiles dropped) but not whole', () => {
  const out = [];
  const one = heavyStage(0);
  const fullSize = JSON.stringify(one).length;
  const strippedSize = JSON.stringify(Object.assign({}, one, { profiles: [] })).length;
  assert.ok(strippedSize < fullSize, 'fixture must actually be lighter without profiles');
  const budget = strippedSize + 5;   // fits stripped, not whole
  const { spent, truncated } = takeStagesBudgeted(out, [one], 32, budget);
  assert.strictEqual(out.length, 1);
  assert.deepStrictEqual(out[0].profiles, []);
  assert.strictEqual(out[0].name, 'heavy0', 'name/order/columns survive the strip');
  assert.strictEqual(spent, strippedSize);
  assert.strictEqual(truncated, false);
});

check('takeStagesBudgeted: once one stage is left out for size, later ones are too', () => {
  const out = [];
  const s0 = heavyStage(0), s1 = heavyStage(1), s2 = heavyStage(2);
  const s0Size = JSON.stringify(s0).length;
  // Enough for exactly one full stage, nothing for a second (even stripped,
  // since a stripped heavy-profile stage is still bigger than 10 bytes).
  const { spent, truncated } = takeStagesBudgeted(out, [s0, s1, s2], 32, s0Size + 10);
  assert.deepStrictEqual(out.map(s => s.name), ['heavy0']);
  assert.strictEqual(truncated, true);
  assert.strictEqual(spent, s0Size);
});

check('takeStagesBudgeted: still respects the count ceiling under a huge budget', () => {
  const out = [];
  const many = [];
  for (let i = 0; i < 40; i++) many.push({ name: 's' + i, order: i, rows: 1, columns: [], profiles: [], inputs: null, read_files: [], ms: 0.1 });
  const { truncated } = takeStagesBudgeted(out, many, 32, Number.MAX_SAFE_INTEGER);
  assert.strictEqual(out.length, 32);
  assert.strictEqual(truncated, false, 'the count ceiling is a silent server-mirrored cap, not a budget truncation');
});

check('takeStagesBudgeted: tolerates junk like takeStages does', () => {
  const out = [];
  const { spent, truncated } = takeStagesBudgeted(out, [null, 'x', 7], 32, 1000);
  assert.deepStrictEqual(out, []);
  assert.strictEqual(spent, 0);
  assert.strictEqual(truncated, false);
});

// ── observePayload ────────────────────────────────────────────────────
check('observePayload: files only (a mid-run flush)', () => {
  const p = observePayload([{ name: 'a.xlsx' }], null, false);
  assert.strictEqual(p.type, 'observe');
  assert.strictEqual(p.files.length, 1);
  assert.ok(!('stages' in p), 'no stages key mid-run');
  assert.ok(!('budget' in p));
});

check('observePayload: stages ride the final flush', () => {
  const p = observePayload([], [stage(0)], false);
  assert.deepStrictEqual(p.files, []);
  assert.strictEqual(p.stages.length, 1);
  assert.strictEqual(p.stages[0].name, 's0');
});

check('observePayload: files and stages travel together', () => {
  const p = observePayload([{ name: 'a.xlsx' }], [stage(0)], true);
  assert.strictEqual(p.files.length, 1);
  assert.strictEqual(p.stages.length, 1);
  assert.strictEqual(p.budget, true);
});

check('observePayload: nothing to say -> no message', () => {
  assert.strictEqual(observePayload([], null, false), null);
  assert.strictEqual(observePayload([], [], false), null);
  assert.strictEqual(observePayload([], [], true), null, 'a budget flag alone is not worth a message');
});

// ── byteLength: UTF-8 bytes, not UTF-16 code units (matches the server's
// own _MAX_OBS_BODY check on real bytes) ────────────────────────────────
check('byteLength: ASCII is 1 byte per char, same as .length', () => {
  assert.strictEqual(byteLength('abc'), 3);
  assert.strictEqual(byteLength('abc'), 'abc'.length);
});

check('byteLength: Cyrillic is 2 bytes per char, double .length', () => {
  // Cyrillic on purpose: two UTF-8 bytes per char is the point of the test.
  const s = 'Филиал';
  assert.strictEqual(s.length, 6);
  assert.strictEqual(byteLength(s), 12);
});

check('takeStagesBudgeted: a Cyrillic-heavy stage is charged its real UTF-8 size, '
  + 'not the smaller UTF-16 code-unit count', () => {
  // Cyrillic on purpose: the payload must cost more in UTF-8 than in UTF-16.
  const st = { name: 'обработка', order: 0, rows: 1, columns: ['Филиал', 'Сумма'],
               profiles: [], inputs: null, read_files: [], ms: 0.1 };
  const codeUnits = JSON.stringify(st).length;
  const bytes = byteLength(JSON.stringify(st));
  assert.ok(bytes > codeUnits, 'Cyrillic payload must cost more in real bytes than in .length');
  const out = [];
  // A budget that fits the (wrongly) smaller code-unit count but not the
  // real byte count must still be rejected as too small.
  const { spent, truncated } = takeStagesBudgeted(out, [st], 32, codeUnits);
  assert.strictEqual(out.length, 0, 'must not fit a stage whose real UTF-8 size exceeds the budget');
  assert.strictEqual(truncated, true);
  assert.strictEqual(spent, 0);
});

console.log('\n' + n + ' checks passed');
