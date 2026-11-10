/**
 * The verbatim guard port, case for case against the Python suite.
 *
 * Every `accepts:`/`rejects:` here is copied from
 * `tests/smoke_explore_py.py::test_guard_accepts` / `test_guard_rejects`. The
 * two engines share one SQL box and one LLM; a string one of them runs and the
 * other refuses is a bug in whichever is looser.
 *
 * Node only — no DuckDB, no wasm: the guard is pure text.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  guardSql, scan, statements, namesIn, lit, checkTable,
  SQLGuardError, ExploreError, FORBIDDEN_KEYWORDS, FORBIDDEN_CALLABLE,
} from '../../static/js/explore2/guard.js';

test('guard accepts what smoke_explore_py accepts', () => {
  // The Cyrillic identifier and comment are deliberate: the scanner must handle non-ASCII text.
  for (const sql of [
    'SELECT 1',
    '  select * from sales_orders where "сумма" > 10  ',
    'WITH x AS (SELECT 1 AS a) SELECT * FROM x',
    'SELECT * FROM sales_orders; ',            // trailing ; only
    'SELECT 1 -- комментарий\n',
    'SELECT /* note */ 1',
  ]) {
    assert.doesNotThrow(() => guardSql(sql), `should accept: ${sql}`);
  }
  // A literal is data, not syntax: these are legal SELECTs and rejecting them
  // would make the guard useless in a Russian 1C corpus full of dashes and
  // English words.
  assert.equal(guardSql("SELECT * FROM t WHERE note = '-- drop table; copy'"),
    "SELECT * FROM t WHERE note = '-- drop table; copy'");
  // replace() is an everyday string function; a keyword blacklist that eats it
  // makes the guard unusable rather than safe.
  assert.ok(guardSql("SELECT replace(a, 'x', 'y') FROM t").startsWith('SELECT replace'));
  // A column genuinely named after a DDL/DML keyword stays queryable.
  assert.ok(guardSql('SELECT "delete" FROM t').startsWith('SELECT'));
  assert.ok(guardSql('SELECT "delete", "set" FROM sales_orders').startsWith('SELECT'));
  // A parenthesised SELECT is still a SELECT.
  assert.doesNotThrow(() => guardSql('(SELECT 1)'));
});

test('guard rejects what smoke_explore_py rejects', () => {
  const cases = {
    'rejects INSERT': 'INSERT INTO sales_orders VALUES (1)',
    'rejects ATTACH': "ATTACH '/etc/passwd' AS x",
    'rejects COPY': "COPY sales_orders TO '/tmp/leak.csv'",
    'rejects PRAGMA': 'PRAGMA database_list',
    'rejects multiple statements': 'SELECT 1; DROP TABLE sales_orders',
    'rejects DML hidden behind a line comment': 'SELECT 1 --\n; DELETE FROM sales_orders',
    'rejects DML hidden behind a block comment': 'SELECT 1 /* x */; UPDATE sales_orders SET a = 1',
    'rejects a comment-split keyword': 'SEL/**/ECT 1',
    'rejects SET inside a SELECT': "SELECT 1 FROM (SET memory_limit='16GB')",
    'rejects a filesystem reader': "SELECT * FROM read_csv('/etc/passwd')",
    'rejects read_parquet': "SELECT * FROM read_parquet('h:/x.parquet')",
    'rejects getenv': "SELECT getenv('HOME')",
    'rejects INSTALL/LOAD of an extension': 'INSTALL httpfs',
    'rejects CREATE': 'CREATE TABLE x AS SELECT 1',
    'rejects an unterminated quote': "SELECT 'abc",
    'rejects an unterminated block comment': 'SELECT 1 /* abc',
    'rejects the empty query': '   \n  ',
    'rejects an oversized query': 'SELECT ' + '1,'.repeat(20000) + '1',
    // A forbidden function name written as a QUOTED identifier is still a real
    // call — DuckDB resolves `"read_csv"(...)` exactly like `read_csv(...)`.
    'rejects a quoted filesystem reader': 'SELECT * FROM "read_csv"(\'/etc/passwd\')',
    'rejects a quoted glob': 'SELECT * FROM "glob"(\'/*\')',
    'rejects a quoted getenv': 'SELECT "getenv"(\'HOME\')',
    'rejects current_setting': "SELECT current_setting('home_directory')",
    'rejects duckdb_settings': 'SELECT * FROM duckdb_settings()',
    'rejects pragma_database_list': 'SELECT * FROM pragma_database_list()',
  };
  for (const [label, sql] of Object.entries(cases)) {
    assert.throws(() => guardSql(sql), SQLGuardError, label);
  }
  assert.throws(() => guardSql(null), SQLGuardError);
});

test('the forbidden tables are the Python tables, not a subset', () => {
  // Spot-check the entries the prototype's concept port did NOT have — the
  // reason docs/ENGINE2.md called it "not ready for a user's SQL box".
  for (const w of ['truncate', 'merge', 'upsert', 'checkpoint', 'vacuum', 'analyze',
    'begin', 'commit', 'rollback', 'transaction', 'prepare', 'execute',
    'deallocate', 'grant', 'revoke', 'use', 'force', 'detach', 'reset']) {
    assert.ok(FORBIDDEN_KEYWORDS.includes(w), `keyword table is missing ${w}`);
  }
  for (const w of ['read_blob', 'parquet_scan', 'csv_scan', 'read_json_objects',
    'load_extension', 'install_extension', 'duckdb_extensions', 'duckdb_functions',
    'pragma_table_info', 'pragma_version', 'pragma_show', 'pragma_collations']) {
    assert.ok(FORBIDDEN_CALLABLE.includes(w), `callable table is missing ${w}`);
  }
  // `replace` is deliberately NOT forbidden — see the comment in the tables.
  assert.ok(!FORBIDDEN_KEYWORDS.includes('replace'));
});

test('_scan preserves length when it is not stripping comments', () => {
  // `statements()` indexes the masked text back into the original; a scan that
  // changed the length would split on the wrong semicolon.
  for (const s of ["SELECT 'a''b', \"c\"\"d\", $q$ raw ; text $q$ FROM t",
    "SELECT 1 -- x\n, 2", 'SELECT /* c */ 1']) {
    assert.equal(scan(s, false, true).length, s.length, s);
    assert.equal(scan(s, false, false).length, s.length, s);
  }
});

test('statements splits only outside quoting', () => {
  assert.equal(statements("SELECT ';' AS a").length, 1);
  assert.equal(statements('SELECT 1; SELECT 2').length, 2);
  assert.equal(statements('SELECT 1;   ').length, 1);
  assert.equal(statements('$t$a;b$t$').length, 1);
});

test('dollar-quoted payloads cannot smuggle a keyword', () => {
  assert.doesNotThrow(() => guardSql('SELECT $q$ drop table t $q$ AS note'));
  assert.throws(() => guardSql('SELECT $q$ x'), SQLGuardError);
});

test('namesIn reads identifiers but not string literals', () => {
  const names = ['datasalesorder', 'datarealisation'];
  assert.deepEqual(namesIn('SELECT * FROM "datasalesorder"', names), ['datasalesorder']);
  assert.deepEqual(namesIn("SELECT * FROM t WHERE note = 'datasalesorder'", names), []);
  assert.deepEqual(namesIn('SELECT * FROM datasalesorder_v2', names), []);
});

test('lit quotes every shape the intent layer can hand it', () => {
  assert.equal(lit("x' OR 1=1"), "'x'' OR 1=1'");
  assert.equal(lit(7), '7');
  assert.equal(lit(true), 'TRUE');
  assert.equal(lit(NaN), 'NULL');
  assert.equal(lit(null), "''");
});

test('checkTable refuses the engine prefix and anything unquotable', () => {
  assert.equal(checkTable(' orders '), 'orders');
  // Cyrillic on purpose: a non-ASCII table name is legal.
  assert.equal(checkTable('таблица'), 'таблица');
  for (const bad of ['_ep_1', 'a"b', 'a;b', 'a b', '', null, 'x'.repeat(65)]) {
    assert.throws(() => checkTable(bad), ExploreError, String(bad));
  }
});
