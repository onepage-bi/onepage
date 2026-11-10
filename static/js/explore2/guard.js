import { t } from './i18n.js';
/**
 * OnePage /explore engine v2 — the SQL guard.
 *
 * A **verbatim** port of `static/py/_explore.py`'s trust boundary: `_scan`,
 * `_statements`, `_FORBIDDEN_KEYWORDS`, `_FORBIDDEN_CALLABLE`, `guard_sql`.
 * Not a re-interpretation — the two files must refuse and accept exactly the
 * same strings, because the same editable SQL box and the same LLM write into
 * both engines. `tests/engine2/guard.test.mjs` mirrors
 * `tests/smoke_explore_py.py::test_guard_accepts/test_guard_rejects` case for
 * case; if you change a list here, change it there and in the Python file.
 *
 * The prototype (engine.js, workstream H1) shipped a *concept* port that kept
 * the load-bearing ordering but not the tables. docs/ENGINE2.md said "port
 * those verbatim before this touches a user's SQL box". This is that port.
 */

export const MAX_SQL_CHARS = 20000;

export class ExploreError extends Error {}
export class SQLGuardError extends ExploreError {}

// ── Identifiers (port of _explore.py::_check_table / _qi / _lit) ─────────
const TABLE_RE = /^[0-9A-Za-z_Ѐ-ӿ]{1,64}$/;
export const RESERVED_PREFIX = '_ep_';

export function checkTable(name) {
  const s = name == null ? '' : String(name).trim();
  if (!TABLE_RE.test(s)) throw new ExploreError(t('Invalid table name: {p1}', {p1: JSON.stringify(name)}));
  if (s.toLowerCase().startsWith(RESERVED_PREFIX)) {
    throw new ExploreError(t('Table name reserved by the engine: {p1}', {p1: JSON.stringify(name)}));
  }
  return s;
}

/** Quote an identifier. The name is already validated; doubling the quote is a
 *  second line of defence, not the first. */
export function qi(name) {
  return '"' + String(name).replace(/"/g, '""') + '"';
}

/** Port of `_explore._lit` — a SQL literal for a JS value. Booleans and finite
 *  numbers go through bare; everything else is a single-quoted string with its
 *  quotes doubled. */
export function lit(v) {
  if (v === true) return 'TRUE';
  if (v === false) return 'FALSE';
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : 'NULL';
  if (typeof v === 'bigint') return String(v);
  return "'" + String(v == null ? '' : v).replace(/'/g, "''") + "'";
}

// ── Forbidden words ──────────────────────────────────────────────────────
// Copied from _explore.py, tuple order and all. The two halves are trusted
// differently and are therefore scanned differently (see `guardSql`).
export const FORBIDDEN_KEYWORDS = [
  // transactions / DDL / DML
  'attach', 'detach', 'insert', 'update', 'delete', 'drop', 'create',
  'alter', 'truncate', 'merge', 'upsert',
  // NOT "replace": `replace()` is an everyday string function, and the only
  // dangerous spellings of the word — CREATE OR REPLACE, INSERT OR REPLACE —
  // are already refused by "create"/"insert".
  // engine control (statement keywords, not callable names)
  'pragma', 'set', 'reset', 'install', 'load', 'copy', 'export',
  'import', 'checkpoint', 'vacuum', 'analyze', 'begin', 'commit',
  'rollback', 'transaction', 'prepare', 'execute', 'deallocate',
  'grant', 'revoke', 'use', 'force',
];
export const FORBIDDEN_CALLABLE = [
  // engine control reachable as a function/table-function call
  'call',
  // filesystem / environment readers that are functions, not statements
  'read_csv', 'read_csv_auto', 'read_parquet', 'read_json',
  'read_json_auto', 'read_json_objects', 'read_text', 'read_blob',
  'parquet_scan', 'csv_scan', 'sniff_csv', 'glob', 'getenv',
  'load_extension', 'install_extension', 'duckdb_extensions',
  // introspection / configuration disclosure (PRAGMA via function call)
  'current_setting', 'duckdb_settings', 'duckdb_functions',
  'pragma_database_list', 'pragma_table_info', 'pragma_version',
  'pragma_show', 'pragma_collations',
];
export const FORBIDDEN = FORBIDDEN_KEYWORDS.concat(FORBIDDEN_CALLABLE);

function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

/** Port of `_explore._forbidden_re`: longest alternative first, word-bounded
 *  on both sides by the SQL identifier charset (NOT \b — `_` is a word char
 *  in both languages but `-` is not, and DuckDB allows neither in a bare
 *  keyword). */
function forbiddenRe(words) {
  const sorted = words.slice().sort((a, b) => b.length - a.length || (a < b ? -1 : 1));
  return new RegExp('(?<![A-Za-z0-9_])(' + sorted.map(escapeRe).join('|') + ')(?![A-Za-z0-9_])', 'i');
}

const FORBIDDEN_KEYWORDS_RE = forbiddenRe(FORBIDDEN_KEYWORDS);
const FORBIDDEN_CALLABLE_RE = forbiddenRe(FORBIDDEN_CALLABLE);

const DOLLAR_RE = /^\$[A-Za-z_0-9]*\$/;

/**
 * One character-level pass over `sql`, honouring quoting — the port of
 * `_explore._scan`.
 *
 * `maskIdents=false` keeps DOUBLE-quoted text (SQL identifiers) readable
 * while still blanking single- and dollar-quoted STRING literals. That is
 * what `referenced()` needs: every statement the query builder and the
 * recipes emit quotes its table name (`FROM "datasalesorder"`), so masking
 * identifiers too would leave the partition badge with nothing to find,
 * while un-masking string literals would let `WHERE note = 'datasalesorder'`
 * invent a table. The guard itself always masks both.
 *
 * Quoting handled: single-quoted strings (with `''` escapes), double-quoted
 * identifiers (with `""`), and `$tag$ ... $tag$` dollar quotes. With
 * `stripComments=false` the output has EXACTLY the same length as the input —
 * `statements()` depends on that to index semicolons back into the original.
 */
export function scan(sql, stripComments, maskLiterals, maskIdents = true) {
  const out = [];
  let i = 0;
  const n = sql.length;
  while (i < n) {
    const ch = sql[i];
    if (ch === "'" || ch === '"') {
      const q = ch;
      const body = [];
      i += 1;
      let closed = false;
      while (i < n) {
        const c = sql[i];
        if (c === q) {
          if (i + 1 < n && sql[i + 1] === q) {
            body.push(c);
            body.push(c);
            i += 2;
            continue;
          }
          i += 1;
          closed = true;
          break;
        }
        body.push(c);
        i += 1;
      }
      if (!closed) throw new SQLGuardError(t('Unclosed quote in SQL.'));
      let text = body.join('');
      if (maskLiterals && (q === "'" || maskIdents)) {
        // Keep the length (error offsets stay meaningful) but make the
        // contents unsearchable: a literal must never trip a keyword.
        text = ' '.repeat(text.length);
      }
      out.push(q + text + q);
      continue;
    }
    if (ch === '$') {
      const m = DOLLAR_RE.exec(sql.slice(i));
      if (m) {
        const tag = m[0];
        const end = sql.indexOf(tag, i + tag.length);
        if (end === -1) throw new SQLGuardError(t('Unclosed string $...$ in SQL.'));
        let body = sql.slice(i + tag.length, end);
        if (maskLiterals) body = ' '.repeat(body.length);
        out.push(tag + body + tag);
        i = end + tag.length;
        continue;
      }
    }
    if (stripComments && ch === '-' && i + 1 < n && sql[i + 1] === '-') {
      const j = sql.indexOf('\n', i);
      out.push(' ');
      i = j === -1 ? n : j;
      continue;
    }
    if (stripComments && ch === '/' && i + 1 < n && sql[i + 1] === '*') {
      const j = sql.indexOf('*/', i + 2);
      if (j === -1) throw new SQLGuardError(t('Unclosed comment /* in SQL.'));
      out.push(' ');
      i = j + 2;
      continue;
    }
    out.push(ch);
    i += 1;
  }
  return out.join('');
}

/** Split on `;` OUTSIDE any quoting, dropping empties — port of
 *  `_explore._statements`. */
export function statements(sql) {
  const parts = [];
  let buf = [];
  const masked = scan(sql, false, true);
  // `masked` has exactly the same length as `sql`, so its semicolons index
  // straight back into the original text.
  for (let i = 0; i < sql.length; i++) {
    if (masked[i] === ';') {
      parts.push(buf.join(''));
      buf = [];
      continue;
    }
    buf.push(sql[i]);
  }
  parts.push(buf.join(''));
  return parts.filter(p => p.trim());
}

/**
 * The trust boundary. Returns the single sanitised statement to execute.
 *
 * Order is the whole point: comments are stripped FIRST, so
 * `SELECT 1 --\n; DROP TABLE t` cannot present itself as one statement, and
 * statements are counted SECOND, so `;` cannot smuggle a payload past a
 * prefix check. Only then is the survivor required to start with SELECT/WITH
 * and to contain no forbidden word outside a literal. The sanitised text —
 * not the caller's original — is what runs.
 */
export function guardSql(sql) {
  if (sql === null || sql === undefined) throw new SQLGuardError(t('Empty query.'));
  const text = String(sql);
  if (text.length > MAX_SQL_CHARS) {
    throw new SQLGuardError(t('Query longer than {p1} characters — shorten it.', {p1: MAX_SQL_CHARS}));
  }
  const clean = scan(text, true, false);
  const stmts = statements(clean);
  if (!stmts.length) throw new SQLGuardError(t('Empty query.'));
  if (stmts.length > 1) {
    throw new SQLGuardError(t('Only one query allowed — remove «;» and everything after.'));
  }
  const stmt = stmts[0].trim();
  // Two scans, deliberately different on identifier-masking (see
  // FORBIDDEN_KEYWORDS / FORBIDDEN_CALLABLE above for why):
  //   maskedIdents   — double-quoted text blanked too, so a bare/unquoted
  //                     DDL/DML keyword is caught but `"delete"` used as an
  //                     ordinary column reference is not.
  //   readableIdents — double-quoted text left readable, so a forbidden
  //                     function spelled as a quoted identifier
  //                     (`"read_csv"('/etc/passwd')`) cannot hide from the
  //                     scan the way it would if identifiers were masked.
  const maskedIdents = scan(stmt, false, true, true);
  const readableIdents = scan(stmt, false, true, false);
  if (!/^\s*\(*\s*(select|with)(?![A-Za-z0-9_])/i.test(maskedIdents)) {
    // The same text as in static/py/_explore.py: the two engines must
    // answer identically, otherwise the same query is explained to the
    // user differently depending on which engine happened to load.
    throw new SQLGuardError(t('Only SELECT and WITH queries are allowed. This looks like a fragment — copy the whole statement, from SELECT or WITH through to the «;».'));
  }
  const hit = FORBIDDEN_KEYWORDS_RE.exec(maskedIdents) || FORBIDDEN_CALLABLE_RE.exec(readableIdents);
  if (hit) {
    throw new SQLGuardError(
      t('Forbidden construct in SQL: {p1}. Explore can only read.', {p1: hit[1].toUpperCase()}));
  }
  return stmt;
}

/**
 * Which of `names` this statement mentions — the port of
 * `_explore._referenced`'s matcher.
 *
 * Deliberately crude — a masked word-boundary match over the known names. It
 * drives a BADGE ("5 of 7 partitions"), never a permission decision, so a
 * false positive costs an extra line in the badge and a false negative costs
 * a missing one.
 */
export function namesIn(stmt, names) {
  const masked = scan(stmt, true, true, false).toLowerCase();
  const hits = [];
  for (const name of names) {
    const re = new RegExp('(?<![A-Za-z0-9_\\u0400-\\u04FF])' + escapeRe(String(name).toLowerCase())
      + '(?![A-Za-z0-9_\\u0400-\\u04FF])');
    if (re.test(masked)) hits.push(name);
  }
  return hits;
}
