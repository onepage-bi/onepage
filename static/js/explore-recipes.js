/* Explore — recipe SQL generators (pure functions, no DOM, no fetch).
 *
 * Each recipe takes plain JS values (table names, column names, chosen
 * partition literals) already resolved from the catalog snapshot the page
 * holds in memory, and returns a single SELECT/WITH string ready to drop
 * into the SQL editor. Nothing here executes SQL or talks to the worker —
 * that stays in explore.js, which only pipes the returned string into the
 * editor (never auto-runs it, same as nl2sql).
 *
 * Quoting: DuckDB identifiers with spaces/Cyrillic/mixed case need double
 * quotes; a literal double-quote inside an identifier is escaped by
 * doubling it (SQL standard, same as DuckDB). String literals use single
 * quotes with the same doubling rule.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.OP_EXPLORE_RECIPES = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function qid(name) {
    return '"' + String(name == null ? '' : name).replace(/"/g, '""') + '"';
  }
  function qlit(v) {
    return "'" + String(v == null ? '' : v).replace(/'/g, "''") + "'";
  }

  // 1. "What changed" — two partitions of one table: added/removed/changed
  //    by a chosen key column. Compares all shared non-key columns.
  function whatChanged(table, keyCol, partA, partB, compareCols) {
    const tv = qid(table);
    const k = qid(keyCol);
    const cols = (compareCols || []).filter(c => c !== keyCol);
    const changedExpr = cols.length
      ? cols.map(c => `a.${qid(c)} IS DISTINCT FROM b.${qid(c)}`).join(' OR ')
      : 'FALSE';
    const status = qid(t('Status'));
    return [
      `WITH a AS (SELECT * FROM ${tv} WHERE __partition = ${qlit(partA)}),`,
      `     b AS (SELECT * FROM ${tv} WHERE __partition = ${qlit(partB)})`,
      `SELECT COALESCE(a.${k}, b.${k}) AS ${k},`,
      t('  CASE WHEN a.{p1} IS NULL THEN \'added\'', {p1: k}),
      t('       WHEN b.{p1} IS NULL THEN \'removed\'', {p1: k}),
      t('       WHEN {p1} THEN \'changed\'', {p1: changedExpr}),
      t('       ELSE \'no change\' END AS {p1}', {p1: status}),
      `FROM a FULL OUTER JOIN b ON a.${k} = b.${k}`,
      `WHERE a.${k} IS NULL OR b.${k} IS NULL OR (${changedExpr})`,
      `ORDER BY ${status}, ${k}`,
    ].join('\n');
  }

  // 2. "As of date" — snapshot of a table as of one chosen partition.
  function asOfDate(table, partition) {
    return `SELECT * FROM ${qid(table)} WHERE __partition = ${qlit(partition)}`;
  }

  // 3. "Reconciliation" — anti-join two tables on a key: rows present in the left
  //    table with no matching key in the right table. Both sides are scoped
  //    to one partition each (when given) — the view behind each table name
  //    is a UNION ALL of every loaded partition, so an unscoped join would
  //    count a row missing from B once per partition of A that still
  //    contains it (a false multiplication of the reconciliation result).
  function reconcile(tableA, tableB, keyColA, keyColB, partA, partB) {
    const ka = qid(keyColA);
    const kb = qid(keyColB || keyColA);
    const sideA = partA
      ? `(SELECT * FROM ${qid(tableA)} WHERE __partition = ${qlit(partA)})`
      : qid(tableA);
    const sideB = partB
      ? `(SELECT * FROM ${qid(tableB)} WHERE __partition = ${qlit(partB)})`
      : qid(tableB);
    return [
      `SELECT a.*`,
      `FROM ${sideA} a`,
      `LEFT JOIN ${sideB} b ON a.${ka} = b.${kb}`,
      `WHERE b.${kb} IS NULL`,
      `ORDER BY a.${ka}`,
    ].join('\n');
  }

  // 4. "Key duplicates/gaps" — duplicate key values and NULL/blank keys
  //    within one partition (when given). Unscoped, this recipe would run
  //    over the UNION ALL of every loaded partition, so a key that is unique
  //    inside each snapshot would still be reported as a duplicate once per
  //    extra partition — a 100% false-positive report on any table with 2+
  //    indexed partitions.
  function dupsAndGaps(table, keyCol, partition) {
    const tv = qid(table);
    const k = qid(keyCol);
    const n = qid(t('Qty'));
    const where = partition ? [`WHERE __partition = ${qlit(partition)}`] : [];
    return [
      `SELECT ${k}, COUNT(*) AS ${n}`,
      `FROM ${tv}`,
      ...where,
      `GROUP BY ${k}`,
      `HAVING COUNT(*) > 1 OR ${k} IS NULL`,
      `ORDER BY ${n} DESC`,
    ].join('\n');
  }

  // 5. "Trend across partitions" — union of partitions, one aggregate row per
  //    partition (default COUNT(*), or SUM(metricCol) when given).
  function trendByPartition(table, metricCol) {
    const tv = qid(table);
    const metric = metricCol
      ? `SUM(${qid(metricCol)}) AS ${qid('Итого')}`
      : `COUNT(*) AS ${qid('Кол-во')}`;
    return [
      `SELECT __partition, ${metric}`,
      `FROM ${tv}`,
      `GROUP BY __partition`,
      `ORDER BY __partition`,
    ].join('\n');
  }

  return { qid, qlit, whatChanged, asOfDate, reconcile, dupsAndGaps, trendByPartition };
});
