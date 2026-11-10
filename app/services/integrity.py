"""Referential integrity: the FK graph read off the models, and the cascades
the delete paths need now that SQLite enforces it (`foreign_keys=ON`, 2026-09-06).

Why explicit deletes and not `ondelete="CASCADE"` on the columns: SQLite keeps
a foreign key's clause inside the table's own `CREATE TABLE` text, so adding
`ON DELETE` to an existing column means rebuilding the table
(create-copy-drop-rename) — a large, risky migration on a live production
database, for a rule the application can state once in Python and pin with a
test. The graph itself is still read from `Base.metadata`, never from a hand
list, so a foreign key added tomorrow is covered the day it is added.

The policy, applied downwards from the row being deleted:

  * a NOT NULL child column exists only because its parent does — the row is
    DELETED, and its own children with it (a dashboard's runs, and the
    observations, files and quality results hanging off those runs);
  * a nullable child column is a reference, not ownership — it is SET NULL and
    the row survives (a dashboard outlives its owner's account, an audit line
    outlives the actor it names, a slide outlives the report it pictured).

`detach()` clears everything pointing AT a row; deleting the row itself stays
with the caller, which is where the audit line and the files on disk are.

Every place in the app that deletes a row which is a PARENT in the graph above
was walked when the pragma went on, and this is the whole list: `users`,
`dashboards` and `presentations` go through `detach()`; `folders` and
`reference_tables` already cleared their own children inline and keep doing it
(the folder's dashboards fall back to the root, and the count of versions goes
into the audit line); `quality.prune()` deletes `quality_results` before the
`source_files` they hang off. Nothing deletes a `generator`, a
`generator_version`, a `generation_run` or a `data_source` at all — a run is
cancelled by status, never removed — so those parents have no path to cover.
Everything else deleted by a route (`share_tokens`, `dashboard_permissions`,
`dashboard_files`, `annotations`, `glossary_terms`, `quality_acks`,
`explore_questions`, `llm_access`, `sales_scopes`) is a leaf.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.models import Base

# Deep enough for the longest chain the schema has
# (dashboards → generation_runs → source_observations → source_files →
# quality_results = 4), with room for one to be added.
_MAX_DEPTH = 8
_CHUNK = 400          # SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999


@dataclass(frozen=True)
class Edge:
    """One foreign key: `child_table.child_col` → `parent_table.parent_col`."""
    child_table: str
    child_col: str
    parent_table: str
    parent_col: str
    nullable: bool


@lru_cache(maxsize=1)
def fk_edges() -> tuple[Edge, ...]:
    """Every foreign key declared in the models, in dependency order."""
    out: list[Edge] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            for fk in col.foreign_keys:
                out.append(Edge(
                    child_table=table.name,
                    child_col=col.name,
                    parent_table=fk.column.table.name,
                    parent_col=fk.column.name,
                    nullable=bool(col.nullable),
                ))
    return tuple(out)


def table_order() -> list[str]:
    """Table names, parents before children (`metadata.sorted_tables`)."""
    return [t.name for t in Base.metadata.sorted_tables]


def children_of(parent_table: str, parent_col: str) -> list[Edge]:
    return [e for e in fk_edges()
            if e.parent_table == parent_table and e.parent_col == parent_col]


def pk_col(table_name: str) -> str | None:
    """The table's single-column primary key, or None if it has none/several."""
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return None
    cols = list(table.primary_key.columns)
    return cols[0].name if len(cols) == 1 else None


# ── clearing the references to a row about to be deleted ─────────────────

def detach(db: Session, table: str, col: str, value) -> dict[str, int]:
    """Clear every row that points at `table.col = value`, recursively.

    Returns `{"<table>.<column>": n}` for deletions and
    `{"<table>.<column>=NULL": n}` for nullifications — small enough to put
    straight into the audit line's detail, and the only place that says what a
    delete actually took with it. Does NOT delete the row itself and does NOT
    commit: the caller owns the transaction."""
    counts: dict[str, int] = {}
    _walk(db, table, col, [value], counts, 0)
    return counts


def _walk(db: Session, table: str, col: str, values: list, counts: dict[str, int], depth: int) -> None:
    if not values or depth >= _MAX_DEPTH:
        return
    for edge in children_of(table, col):
        if edge.nullable:
            n = _nullify(db, edge, values)
            if n:
                key = f"{edge.child_table}.{edge.child_col}=NULL"
                counts[key] = counts.get(key, 0) + n
            continue
        # NOT NULL: the row cannot outlive its parent. Take its own children
        # first — under foreign_keys=ON the delete below would be refused.
        child_pk = pk_col(edge.child_table)
        if child_pk is not None and children_of(edge.child_table, child_pk):
            _walk(db, edge.child_table, child_pk,
                  _child_ids(db, edge, child_pk, values), counts, depth + 1)
        n = _delete(db, edge, values)
        if n:
            key = f"{edge.child_table}.{edge.child_col}"
            counts[key] = counts.get(key, 0) + n


def _chunks(values: list) -> list[list]:
    return [values[i:i + _CHUNK] for i in range(0, len(values), _CHUNK)]


def _in(sql: str):
    return text(sql).bindparams(bindparam("vals", expanding=True))


def _child_ids(db: Session, edge: Edge, child_pk: str, values: list) -> list:
    stmt = _in(f'SELECT "{child_pk}" FROM "{edge.child_table}" WHERE "{edge.child_col}" IN :vals')
    out: list = []
    for chunk in _chunks(values):
        out.extend(r[0] for r in db.execute(stmt, {"vals": chunk}))
    return out


def _nullify(db: Session, edge: Edge, values: list) -> int:
    stmt = _in(f'UPDATE "{edge.child_table}" SET "{edge.child_col}" = NULL '
               f'WHERE "{edge.child_col}" IN :vals')
    return sum(db.execute(stmt, {"vals": chunk}).rowcount or 0 for chunk in _chunks(values))


def _delete(db: Session, edge: Edge, values: list) -> int:
    stmt = _in(f'DELETE FROM "{edge.child_table}" WHERE "{edge.child_col}" IN :vals')
    return sum(db.execute(stmt, {"vals": chunk}).rowcount or 0 for chunk in _chunks(values))


# ── orphans (used by tools/fk_orphans.py, on a raw sqlite3 connection) ────

def orphan_where(edge: Edge) -> str:
    """The WHERE clause both the report and the fix use: a non-NULL child value
    with no parent row. Aliased `c` so the caller composes COUNT/DELETE around
    it; the report never SELECTs the value itself, so a run against a
    production copy carries no data out."""
    return (f'c."{edge.child_col}" IS NOT NULL '
            f'AND NOT EXISTS (SELECT 1 FROM "{edge.parent_table}" p '
            f'WHERE p."{edge.parent_col}" = c."{edge.child_col}")')


def orphan_count_sql(edge: Edge) -> str:
    return f'SELECT COUNT(*) FROM "{edge.child_table}" c WHERE {orphan_where(edge)}'


def orphan_delete_sql(edge: Edge) -> str:
    """SQLite has no DELETE ... alias, so the same predicate is re-written
    against the bare table name."""
    where = orphan_where(edge).replace('c."', '"')
    return f'DELETE FROM "{edge.child_table}" WHERE {where}'


def orphan_nullify_sql(edge: Edge) -> str:
    """The nullable half of the same policy as `detach()`: a reference whose
    target is gone is cleared, and the row stays. Only valid for
    `edge.nullable` — a NOT NULL column has to go through
    `orphan_delete_sql()`."""
    where = orphan_where(edge).replace('c."', '"')
    return (f'UPDATE "{edge.child_table}" SET "{edge.child_col}" = NULL '
            f'WHERE {where}')
