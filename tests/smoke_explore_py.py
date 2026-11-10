"""Smoke test for static/py/_explore.py under native CPython.

The same file runs inside the Pyodide worker (gen-worker.js, mode 'explore').
Everything interesting about it — the union of partitions, the header offset,
and above all the SQL guard that is the ONLY thing between an editable SQL box
(which an LLM also writes into) and a DuckDB connection — is plain Python and
would otherwise only be observable by opening a browser tab.

Requires duckdb, openpyxl, pandas and python-calamine in the interpreter
(`uv pip install duckdb` into the venv if missing). Fixtures are built with
openpyxl into a tempdir, so the header-offset case is a real workbook with a
real title block above the table, not a synthetic frame.

    .venv/bin/python tests/smoke_explore_py.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.getcwd(), "static", "py"))
import _explore  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def rejects(label, sql):
    try:
        _explore.guard_sql(sql)
    except _explore.SQLGuardError as e:
        check(label, True)
        return str(e)
    except Exception as e:                       # pragma: no cover
        check(label, False, f"wrong exception type: {e!r}")
        return ""
    check(label, False, "ACCEPTED — this SQL would have executed")
    return ""


# ── fixtures ─────────────────────────────────────────────────────────────
def orders_book(path, rows, *, title_block=0, extra_col=False):
    """One 1C-shaped export. `title_block` blank/title rows sit ABOVE the
    header, which is exactly what makes header_row load-bearing."""
    wb = Workbook()
    ws = wb.active
    ws.title = "TDSheet"
    for i in range(title_block):
        ws.append([f"Report, line {i + 1}"])
    header = [" Order number ", "Amount", "Date"]
    if extra_col:
        header.append("Manager")
    ws.append(header)
    for no, amount, date in rows:
        row = [no, amount, date]
        if extra_col:
            row.append("Ivanov")
        ws.append(row)
    wb.save(path)


def build(tmp):
    """Two partitions of one table; the second has a title block, a different
    header row AND an extra column — the union has to survive all three."""
    a = os.path.join(tmp, "DataSalesOrder_07.26.xlsx")
    b = os.path.join(tmp, "DataSalesOrder_08.26.xlsx")
    orders_book(a, [("A-1", 100, "2026-07-01"), ("A-2", 200, "2026-07-02")])
    orders_book(b, [("B-1", 300, "2026-08-01")], title_block=3, extra_col=True)
    return a, b


CATALOG = {"tables": [{
    "table_id": "t_abc", "name": "sales_orders", "display_name": "Orders",
    "partitions": [{"key": "2026-07"}, {"key": "2026-08"}, {"key": "2026-09"}],
}, {
    "table_id": "t_tim", "name": "shifts", "display_name": "Shifts",
    "partitions": [{"key": "2026-07"}, {"key": "2026-08"}],
}]}


# ── tests ────────────────────────────────────────────────────────────────
def test_union(tmp):
    """Two partitions become one view, tagged with __partition/__file, and the
    header offset of the second file is honoured."""
    a, b = build(tmp)
    _explore.set_catalog(CATALOG)
    ra = _explore.index_file(a, "TDSheet", 0, "sales_orders", "2026-07")
    rb = _explore.index_file(b, "TDSheet", 3, "sales_orders", "2026-08")
    check("index_file rows (partition A)", ra["rows"] == 2, ra)
    check("index_file rows (partition B)", rb["rows"] == 1, rb)
    # The catalogue's norm_col is what the query builder writes SQL against:
    # " Order number " must arrive as "order number", not as the raw header.
    check("columns normalised like the catalogue",
          ra["columns"][:3] == ["order number", "amount", "date"], ra["columns"])
    check("/inbox copy is unlinked after parse", not os.path.exists(a))

    res = _explore.run_sql('SELECT __partition, count(*) AS n FROM sales_orders '
                           'GROUP BY 1 ORDER BY 1')
    check("union: no error", res["error"] is None, res["error"])
    check("union: both partitions present",
          res["rows"] == [["2026-07", 2], ["2026-08", 1]], res["rows"])
    check("union: total counted", res["total"] == 2, res)

    res = _explore.run_sql('SELECT "order number", __file FROM sales_orders '
                           'WHERE __partition = \'2026-08\'')
    check("header_row offset: title block is not data",
          res["rows"] == [["B-1", "DataSalesOrder_08.26.xlsx"]], res["rows"])

    # UNION ALL BY NAME, not positional: the extra column exists and is NULL
    # for the partition that does not have it.
    res = _explore.run_sql('SELECT __partition, "manager" FROM sales_orders ORDER BY 1, 2')
    check("union by name: missing column is NULL, not a shifted value",
          res["rows"] == [["2026-07", None], ["2026-07", None], ["2026-08", "Ivanov"]],
          res["rows"])


def test_context(tmp):
    """The badge data: partitions used vs the catalogue's expectation."""
    res = _explore.run_sql("SELECT * FROM sales_orders")
    check("partitions_used lists what was really read",
          sorted(p["key"] for p in res["partitions_used"]) == ["2026-07", "2026-08"],
          res["partitions_used"])
    check("partitions_expected comes from the catalogue",
          res["partitions_expected"] == 3, res["partitions_expected"])
    check("missing partition named",
          [m["key"] for m in res["missing_partitions"]] == ["2026-09"],
          res["missing_partitions"])
    check("incomplete is a field of the result", res["incomplete"] is True, res)

    # Regression: EVERY statement the query builder and the 5 recipes emit
    # quotes its table name. Masking double-quoted identifiers along with
    # string literals left the badge reading "0 of 0 partitions" for every
    # real query the UI produces.
    res = _explore.run_sql('SELECT * FROM "sales_orders"')
    check("quoted table name still feeds the partition badge",
          sorted(p["key"] for p in res["partitions_used"]) == ["2026-07", "2026-08"],
          res["partitions_used"])
    # ...but a string literal that merely spells a table name must not.
    res = _explore.run_sql("SELECT 'sales_orders' AS note")
    check("a table name inside a string literal invents no partitions",
          res["partitions_used"] == [] and res["error"] is None, res)

    t = _explore.tables()
    check("tables() reports the gap too",
          t and t[0]["missing"] == ["2026-09"] and t[0]["expected"] == 3, t)
    st = _explore.stats()
    check("stats() reports approximate bytes",
          st["rows"] == 3 and st["partitions"] == 2 and st["bytes"] > 0, st)


def test_reindex(tmp):
    """Re-indexing the same file replaces its partition instead of doubling
    every count in every answer."""
    path = os.path.join(tmp, "DataSalesOrder_07.26.xlsx")
    orders_book(path, [("A-1", 100, "2026-07-01"), ("A-2", 200, "2026-07-02")])
    _explore.index_file(path, "TDSheet", 0, "sales_orders", "2026-07")
    res = _explore.run_sql("SELECT count(*) FROM sales_orders")
    check("re-index replaces, never duplicates", res["rows"] == [[3]], res["rows"])


def test_time_union(tmp):
    """A real folder had one partition whose "Time" cells read as pure
    `datetime.time` (DuckDB: TIME) and another where the same column read as
    full datetimes (TIMESTAMP) — and the union view died on DuckDB's
    unimplemented TIME -> TIMESTAMP cast. Times are stringified at index
    time, so the union must survive."""
    import datetime as dt
    a = os.path.join(tmp, "Shifts_07.xlsx")
    b = os.path.join(tmp, "Shifts_08.xlsx")
    for path, val in ((a, dt.time(9, 30)),
                      (b, dt.datetime(2026, 8, 1, 9, 30))):
        wb = Workbook()
        ws = wb.active
        ws.title = "TDSheet"
        ws.append(["Shift", "Time"])
        ws.append(["S-1", val])
        wb.save(path)
    _explore.index_file(a, "TDSheet", 0, "shifts", "2026-07")
    _explore.index_file(b, "TDSheet", 0, "shifts", "2026-08")
    res = _explore.run_sql(
        'SELECT "time" FROM shifts ORDER BY __partition')
    check("TIME/TIMESTAMP partitions union without a cast error",
          res.get("error") is None and len(res["rows"]) == 2, res)
    check("pure time became an ISO string",
          res["rows"] and res["rows"][0][0] == "09:30:00", res["rows"])


def test_semantics(tmp):
    _explore.CACHE_DIR = os.path.join(tmp, "cache-sem")   # materialised views cache here
    """ADR-13 in the worker: semantics bound to the catalogue become `sem`
    views over the indexed partitions; KPI SQL from the file runs over them;
    an intent becomes deterministic SQL the guard accepts and the engine
    executes — with the partition badge still pointing at the real tables."""
    sem = {
        "schema_version": 1, "generator": "semtest",
        "rules": {"reporting_start": "2026-07-01"},
        "tables": {
            "orders": {"description": "Orders", "sources": [{"file": "DataSalesOrder_*.xlsx", "sheet": "TDSheet"}],
                       "columns": {"order_no": {"source": " Order number ", "occurrence": 0, "type": "text", "role": "key"},
                                   "amount": {"source": "Amount", "occurrence": 0, "type": "number", "role": "measure"},
                                   "day": {"source": "Date", "occurrence": 0, "type": "date", "role": "time"},
                                   "manager": {"source": "Manager", "occurrence": 0, "type": "text", "role": "dim"},
                                   "missing": {"source": "No such column", "occurrence": 0, "type": "text", "role": "dim"}}},
            "tiers": {"description": "inline", "sources": [{"object": "cfg"}],
                      "columns": {"manager": {"type": "text", "role": "key"}, "tier": {"type": "text", "role": "attr"}},
                      "rows": [{"manager": "Ivanov", "tier": "gold"}]},
            # ADR-16: a platform reference table. The server attached the
            # current version's rows as LISTS in `reference.columns` order —
            # the worker must materialise them exactly like inline rows and
            # report which version answered.
            "clients": {"description": "Client reference table",
                        "sources": [{"reference": "sl_client_map"}],
                        "business_key": "client",
                        "columns": {"client": {"type": "text", "role": "key"},
                                    "client_display": {"type": "text", "role": "dim"}},
                        "reference": {"slug": "sl_client_map", "status": "ok", "version": 7,
                                      "title": "Clients", "columns": ["client", "client_display"],
                                      "rows": [["romashka", "Daisy Retail"], ["vasilek", "Cornflower"]],
                                      "row_count": 2,
                                      "message": "reference table sl_client_map v7"}},
            # The same declaration with nothing behind it: one honest
            # `unbound`, and the rest of the report keeps answering.
            "regions": {"description": "Regions",
                        "sources": [{"reference": "sl_region_map"}],
                        "business_key": "region",
                        "columns": {"region": {"type": "text", "role": "key"},
                                    "region_display": {"type": "text", "role": "dim"}},
                        "reference": {"slug": "sl_region_map", "status": "missing",
                                      "message": "reference table sl_region_map has not been created"}},
        },
        "views": {"lines": {"sql": "SELECT o.*, coalesce(t.tier, 'none') AS tier FROM orders o LEFT JOIN tiers t USING (manager) WHERE day >= DATE '{reporting_start}'"}},
        "kpis": {"avg_amount": {"name": "Average ticket", "definition": "Σ amount / number of orders", "aggregation": "ratio",
                                "dims": ["tier", "day"], "measure": "ratio",
                                "cell_sql": "SELECT tier, day, SUM(amount) AS n, COUNT(*) AS d FROM lines GROUP BY 1, 2"},
                 "amount_by": {"name": "Amount", "definition": "Σ amount", "aggregation": "sum",
                               "dims": ["manager", "tier", "day"],
                               "cell_sql": "SELECT manager, tier, day, SUM(amount) AS value FROM lines GROUP BY 1, 2, 3"}},
    }
    # The frozen form: placeholders resolved, dims derived (what the server
    # ships after registration — the worker never templates).
    sem["views"]["lines"]["sql"] = sem["views"]["lines"]["sql"].replace("{reporting_start}", "2026-07-01")
    bindings = {"orders": {"inline": False, "matches": [{"table": "sales_orders", "table_id": "t_abc",
                           "files": ["DataSalesOrder_07.26.xlsx", "DataSalesOrder_08.26.xlsx"],
                           # `manager`: None = the server's catalogue never learned the
                           # column (prod SL 2026-08-23: "Customer" bound to nothing, every
                           # client NULL) — the engine must re-resolve it from the header.
                           "columns": {"order_no": "order number", "amount": "amount", "day": "date", "manager": None, "missing": None}}]},
                "tiers": {"inline": True, "matches": []}}
    _explore.set_catalog({**CATALOG, "semantics": [{"generator": "semtest", "generator_id": 7, "semantics": sem, "bindings": bindings}]})
    st = _explore.semantics_status()
    g = (st.get("generators") or [{}])[0]
    check("semantic table bound over the indexed catalogue view", g.get("tables", {}).get("orders") == "bound", st)
    check("inline-row table materialised", g.get("tables", {}).get("tiers") == "inline", g.get("tables"))
    check("reference-backed table materialised with the platform's rows",
          g.get("tables", {}).get("clients") == "reference", g.get("tables"))
    check("a reference nobody created is one unbound table, not an error",
          g.get("tables", {}).get("regions") == "unbound"
          and any("not been created" in e for e in (g.get("errors") or [])), g)
    check("status names the reference version that answered",
          (g.get("references") or []) == [{"table": "clients", "slug": "sl_client_map",
                                           "version": 7, "title": "Clients", "rows": 2}],
          g.get("references"))
    check("reference rows are queryable through the ordinary SQL path",
          _explore.run_sql("SELECT client_display FROM clients WHERE client = 'romashka'")["rows"]
          == [["Daisy Retail"]], _explore.run_sql("SELECT * FROM clients"))
    # The wire copy of the rows is handed to a pandas frame and dropped: a
    # re-index re-materialises everything, and keeping both would double a
    # dictionary that can be megabytes in a tab that never releases it.
    check("the dictionary's rows are moved into a frame, not kept twice",
          "rows" not in sem["tables"]["clients"]["reference"]
          and len(_explore._SEM_REF_FRAMES) == 1,
          str(list(sem["tables"]["clients"]["reference"])))
    _explore._SEM_DIRTY = True
    _explore.semantics_status()
    check("a re-materialisation reuses that frame and still answers",
          _explore.run_sql("SELECT client_display FROM clients WHERE client = 'romashka'")["rows"]
          == [["Daisy Retail"]]
          and len([r for r in _explore._FRAMES if r.startswith("_ep_sem")]) == 2,
          str([r for r in _explore._FRAMES if r.startswith("_ep_sem")]))
    check("declared view created (materialized over the bound table)", g.get("views", {}).get("lines") == "materialized", g)
    check("KPI reported ready", g.get("kpis") and g["kpis"][0].get("ready") is True, g.get("kpis"))
    check("KPI status carries question_forms and dim_synonyms for the chat's guards",
          g.get("kpis") and isinstance(g["kpis"][0].get("question_forms"), list)
          and isinstance(g["kpis"][0].get("dim_synonyms"), dict), g.get("kpis") and g["kpis"][0])
    check("inline table is TYPED like the server's dry run (text stays text, engine cols present)",
          _explore.run_sql("SELECT tier, __file FROM tiers")["error"] is None)
    res = _explore.run_sql("SELECT tier, count(*) AS n FROM lines GROUP BY 1 ORDER BY 1")
    check("view readable through the ordinary SQL path", res["error"] is None and res["rows"], res)
    check("column the catalogue never learned is re-resolved from its source header (not NULL)",
          _explore.run_sql("SELECT count(*) FROM orders WHERE manager IS NOT NULL")["rows"][0][0] > 0)
    # Materialised view is cached as Parquet: a second build of the SAME
    # index comes from the cache and yields identical rows.
    first = _explore.run_sql("SELECT count(*), sum(amount) FROM lines")["rows"]
    _explore._SEM_DIRTY = True
    st2 = _explore.semantics_status()
    g2 = st2["generators"][0]
    check("materialised view rebuilt from the Parquet cache on the next build",
          "lines" in (g2.get("materialized_from_cache") or []), g2.get("materialized_from_cache"))
    check("cached materialisation yields the same rows",
          _explore.run_sql("SELECT count(*), sum(amount) FROM lines")["rows"] == first)
    check("status names the columns that stayed unbound, and only those",
          g.get("unbound_columns", {}).get("orders") == ["missing"], g.get("unbound_columns"))
    check("missing source column is NULL, not an error",
          _explore.run_sql("SELECT count(*) FROM orders WHERE missing IS NULL")["rows"] == [[3]])
    check("catalogue table still readable (search_path keeps main)",
          _explore.run_sql("SELECT count(*) FROM sales_orders")["rows"] == [[3]])
    out = _explore.intent_sql({"kpi": "avg_amount", "generator_id": 7, "dims": ["tier"], "filters": [{"dim": "day", "op": ">=", "value": "2026-07-01"}],
                               "order": {"by": "value", "dir": "desc"}, "limit": 10})
    check("intent -> SQL without error", out.get("error") is None and out.get("sql"), out)
    res = _explore.run_sql(out["sql"]) if out.get("sql") else {"error": "no sql"}
    check("intent SQL passes the guard and runs", res["error"] is None and res["columns"][-1] == "value", res)
    check("badge: semantic query still credits the catalogue partitions",
          res.get("partitions_used") and {p["table"] for p in res["partitions_used"]} == {"sales_orders"}, res.get("partitions_used"))
    check("intent: unknown dim is refused", _explore.intent_sql({"kpi": "avg_amount", "generator_id": 7, "dims": ["nope"]}).get("error"))
    check("intent: unknown kpi is refused", _explore.intent_sql({"kpi": "zzz", "generator_id": 7}).get("error"))
    # ── the engine checks filter VALUES against the data (prod 2026-08-23:
    # «Grando» hung on shipping_display gave an empty answer, no explanation) ──
    out = _explore.intent_sql({"kpi": "avg_amount", "generator_id": 7,
                               "filters": [{"dim": "tier", "op": "=", "value": "GOLD"}]})
    check("filter fix: a case-mismatched value is corrected against the data",
          "= 'gold'" in out["sql"] and any("GOLD" in n for n in out.get("notes") or []),
          str(out.get("notes")) + " | " + (out.get("sql") or "")[-120:])
    out = _explore.intent_sql({"kpi": "amount_by", "generator_id": 7,
                               "filters": [{"dim": "tier", "op": "=", "value": "Ivanov"}]})
    check("filter fix: a value that lives in ANOTHER dim moves the filter there",
          '"manager" = ' in out["sql"] and any("filter moved" in n for n in out.get("notes") or []),
          str(out.get("notes")) + " | " + (out.get("sql") or "")[-140:])
    out = _explore.intent_sql({"kpi": "avg_amount", "generator_id": 7,
                               "filters": [{"dim": "tier", "op": "=", "value": "platinum"}]})
    check("filter fix: a value found nowhere is reported with the dim's known values",
          any("is not in tier" in n for n in out.get("notes") or []), str(out.get("notes")))
    check("intent: literal is quoted, not interpolated",
          "'x'' OR 1=1'" in _explore.intent_sql({"kpi": "avg_amount", "generator_id": 7, "filters": [{"dim": "tier", "op": "=", "value": "x' OR 1=1"}]})["sql"])
    # re-index marks the layer dirty and it rebuilds transparently
    path = os.path.join(tmp, "DataSalesOrder_07.26.xlsx")
    orders_book(path, [("A-1", 100, "2026-07-01"), ("A-2", 200, "2026-07-02")])
    _explore.index_file(path, "TDSheet", 0, "sales_orders", "2026-07")
    check("semantic views survive a re-index", _explore.run_sql("SELECT count(*) FROM orders")["error"] is None)
    # Only the files the source pattern admitted feed the semantic table: a
    # catalogue cluster may hold more files than the pattern names.
    narrow = {**bindings, "orders": {"inline": False, "matches": [{**bindings["orders"]["matches"][0], "files": ["DataSalesOrder_08.26.xlsx"]}]}}
    _explore.set_catalog({**CATALOG, "semantics": [{"generator": "semtest", "generator_id": 7, "semantics": sem, "bindings": narrow}]})
    _explore.semantics_status()
    check("bound view is filtered to the matched partition files (1 of 3 rows)",
          _explore.run_sql("SELECT count(*) FROM orders")["rows"] == [[1]], _explore.run_sql("SELECT count(*) FROM orders"))
    # An unbound table makes every KPI over it NOT ready, and intent_sql refuses it.
    unbound = {**bindings, "orders": {"inline": False, "matches": []}}
    _explore.set_catalog({**CATALOG, "semantics": [{"generator": "semtest", "generator_id": 7, "semantics": sem, "bindings": unbound}]})
    st = _explore.semantics_status()
    g = st["generators"][0]
    check("unbound table -> KPI not ready", g["tables"]["orders"] == "unbound" and g["kpis"][0]["ready"] is False, g)
    check("intent over a not-ready KPI is refused, not handed a Binder Error later",
          "not ready" in (_explore.intent_sql({"kpi": "avg_amount", "generator_id": 7}).get("error") or ""))
    check("intent without generator_id is refused", _explore.intent_sql({"kpi": "avg_amount"}).get("error"))
    _explore.set_catalog({**CATALOG, "semantics": [{"generator": "semtest", "generator_id": 7, "semantics": sem, "bindings": bindings}]})
    _explore.semantics_status()


def test_type_conflict(tmp):
    """1C exports: "Amount" is numeric in one month and ' ' (a blank string) in
    the next. The union used to die on `' ' -> INT32`; now mixed columns
    become VARCHAR, all-numeric ones DOUBLE, and the query runs."""
    _explore.set_catalog({"tables": [{"table_id": "t_mix", "name": "mixed", "display_name": "Mixed",
                                      "partitions": [{"key": "07"}, {"key": "08"}]}]})
    a = os.path.join(tmp, "Mixed_07.xlsx")
    b = os.path.join(tmp, "Mixed_08.xlsx")
    # File b mixes a real number and a blank STRING in one column — the case
    # that made DuckDB guess INT32 from the first rows and die on the ' '.
    for path, rows in ((a, [("M-1", 100, "2026-07-01")]), (b, [("M-2", " ", "2026-08-01"), ("M-3", 250, "2026-08-02")])):
        wb = Workbook()
        ws = wb.active
        ws.title = "TDSheet"
        ws.append(["Number", "Amount", "Date"])
        for r in rows:
            ws.append(list(r))
        wb.save(path)
    _explore.index_file(a, "TDSheet", 0, "mixed", "07")
    _explore.index_file(b, "TDSheet", 0, "mixed", "08")
    res = _explore.run_sql('SELECT "number", "amount" FROM mixed ORDER BY 1')
    check("mixed numeric/text column unions without a cast error", res["error"] is None and len(res["rows"]) == 3, res)
    res = _explore.run_sql('SELECT SUM(TRY_CAST("amount" AS DOUBLE)) AS s FROM mixed')
    check("the semantic-style re-parse sees the numbers and ignores the blank", res["rows"] == [[350.0]], res)
    # Dates mixed with '-' text in ONE column (the invalid timestamp field
    # format: "-" case): the column becomes text, the date survives as ISO.
    import datetime as dt
    c = os.path.join(tmp, "Mixed_09.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "TDSheet"
    ws.append(["Number", "Amount", "Date"])
    ws.append(["M-4", 1, dt.datetime(2026, 9, 1, 10, 30)])
    ws.append(["M-5", 2, "-"])
    wb.save(c)
    _explore.index_file(c, "TDSheet", 0, "mixed", "09")
    res = _explore.run_sql('SELECT "number", "date" FROM mixed WHERE __partition = \'09\' ORDER BY 1')
    check("date + '-' text column indexes without a timestamp cast error", res["error"] is None and len(res["rows"]) == 2, res)
    res = _explore.run_sql('SELECT COUNT(TRY_CAST("date" AS TIMESTAMP)) FROM mixed WHERE __partition = \'09\'')
    check("the real date is still parseable, the dash is NULL", res["rows"] == [[1]], res)


def test_cache(tmp):
    """Parsed partitions are cached as Parquet; a later index serves them
    without the workbook (the 20-minute re-index on an unchanged folder)."""
    _explore.CACHE_DIR = os.path.join(tmp, "cache")
    _explore.set_catalog({"tables": [{"table_id": "t_c", "name": "cached_t", "display_name": "Cache",
                                      "partitions": [{"key": "07"}]}]})
    path = os.path.join(tmp, "Cache_07.xlsx")
    orders_book(path, [("C-1", 100, "2026-07-01"), ("C-2", " ", "2026-07-02")])
    key = _explore.cache_key("Cache_07.xlsx", 1234, 1700000000000, "TDSheet", 0)
    check("cache_key is a sha1", len(key) == 40)
    probe = _explore.cache_probe([key])
    check("probe before: miss", probe[key] is False, probe)
    check("probe reports the parser version the page must match",
          probe["parser_version"] == _explore.PARSER_VERSION, probe)
    out = _explore.index_file(path, "TDSheet", 0, "cached_t", "07", "Cache_07.xlsx", key)
    check("index_file wrote the cache", out.get("cached") is True and os.path.exists(os.path.join(_explore.CACHE_DIR, key + ".parquet")), out)
    before = _explore.run_sql('SELECT "order number", "amount" FROM cached_t ORDER BY 1')["rows"]
    check("probe after: hit", _explore.cache_probe([key])[key] is True)
    _explore.reset()
    _explore.set_catalog({"tables": [{"table_id": "t_c", "name": "cached_t", "display_name": "Cache",
                                      "partitions": [{"key": "07"}]}]})
    out = _explore.index_cached(key, "cached_t", "07", "Cache_07.xlsx", "TDSheet")
    check("index_cached registers without the workbook", out.get("cached") is True and out["rows"] == 2, out)
    after = _explore.run_sql('SELECT "order number", "amount" FROM cached_t ORDER BY 1')["rows"]
    check("cached partition answers identically (incl. the coerced blank)", after == before, (before, after))
    res = _explore.run_sql("SELECT __partition, __file FROM cached_t LIMIT 1")["rows"]
    check("engine columns rebuilt from the registration, not the file", res == [["07", "Cache_07.xlsx"]], res)
    check("user SQL still cannot read the cache files",
          _explore.run_sql(f"SELECT * FROM read_parquet('{os.path.join(_explore.CACHE_DIR, key)}.parquet')")["error"])
    st = _explore.cache_stats()
    check("cache_stats counts the file", st["files"] == 1 and st["bytes"] > 0, st)
    check("cache_clear empties it", _explore.cache_clear()["removed"] == 1 and _explore.cache_stats()["files"] == 0)
    try:
        _explore.index_cached(key, "cached_t", "07", "Cache_07.xlsx", "TDSheet")
        check("index_cached on a missing key raises", False)
    except _explore.ExploreError:
        check("index_cached on a missing key raises", True)


def test_cache_portable(tmp):
    """Phase 9.2: the Parquet a browser parsed is portable — export the bytes,
    import them under another key in a cache that never saw the workbook, and
    the partition registers with identical rows."""
    _explore.reset()
    _explore.CACHE_DIR = os.path.join(tmp, "cache-portable")
    _explore.set_catalog({"tables": [{"table_id": "t_p", "name": "portable_t", "display_name": "Portable",
                                      "partitions": [{"key": "07"}, {"key": "08"}]}]})
    path = os.path.join(tmp, "Portable_07.xlsx")
    orders_book(path, [("P-1", 100, "2026-07-01"), ("P-2", " ", "2026-07-02")])
    key = _explore.cache_key("Portable_07.xlsx", 42, 1700000001000, "TDSheet", 0)
    _explore.index_file(path, "TDSheet", 0, "portable_t", "07", "Portable_07.xlsx", key)
    before = _explore.run_sql('SELECT "order number", "amount" FROM portable_t ORDER BY 1')["rows"]

    blobs = _explore.cache_export([key, "0" * 40])
    check("cache_export returns the parquet bytes for a present key",
          list(blobs) == [key] and blobs[key][:4] == b"PAR1", list(blobs))
    check("cache_export omits keys it does not have", "0" * 40 not in blobs)
    check("cache_export rejects a key of the wrong shape", _explore.cache_export(["../etc/passwd", "ZZ"]) == {})

    # A NEW key: nothing about this partition was ever parsed under it.
    new_key = "b" * 40
    out = _explore.cache_import(new_key, blobs[key])
    check("cache_import accepts validated parquet",
          out == {"ok": True, "bytes": len(blobs[key])}, out)
    check("cache_import wrote the file under the new key",
          os.path.exists(os.path.join(_explore.CACHE_DIR, new_key + ".parquet")))
    check("cache_import leaves no temp file behind",
          [f for f in os.listdir(_explore.CACHE_DIR) if not f.endswith(".parquet")] == [],
          os.listdir(_explore.CACHE_DIR))
    reg = _explore.index_cached(new_key, "portable_t", "08", "Portable_08.xlsx", "TDSheet")
    check("index_cached registers the imported partition", reg.get("cached") is True and reg["rows"] == 2, reg)
    after = _explore.run_sql('SELECT "order number", "amount" FROM portable_t '
                             "WHERE __partition = '08' ORDER BY 1")["rows"]
    check("imported partition answers identically to the parsed one", after == before, (before, after))

    n_before = len(os.listdir(_explore.CACHE_DIR))
    bad = _explore.cache_import("c" * 40, b"not a parquet file at all")
    check("garbage bytes are refused", bad["ok"] is False and bad["error"], bad)
    check("garbage leaves NOTHING in the cache dir",
          len(os.listdir(_explore.CACHE_DIR)) == n_before, os.listdir(_explore.CACHE_DIR))
    # PAR1 magic alone is not enough — DuckDB has to be able to read it.
    fake = _explore.cache_import("d" * 40, b"PAR1" + b"\x00" * 64)
    check("parquet magic without a readable file is still refused",
          fake["ok"] is False and fake["error"], fake)
    check("a rejected DESCRIBE leaves no file either",
          len(os.listdir(_explore.CACHE_DIR)) == n_before, os.listdir(_explore.CACHE_DIR))
    bad_key = _explore.cache_import("../../etc/passwd", blobs[key])
    check("cache_import rejects a bad key", bad_key["ok"] is False and bad_key["error"], bad_key)
    check("a short-but-hex key is portable (another engine's recipe)",
          _explore.cache_import("a" * 16, blobs[key])["ok"] is True)
    check("...and probe/index_cached see it too", _explore.cache_probe(["a" * 16])["a" * 16] is True)


def test_limit(tmp):
    """A limited grid still says how many rows there really were."""
    res = _explore.run_sql("SELECT * FROM sales_orders", limit=2)
    check("limit honoured", len(res["rows"]) == 2, len(res["rows"]))
    check("truncated flagged", res["truncated"] is True, res)
    check("total is the real count behind the limit", res["total"] == 3, res["total"])
    res = _explore.run_sql("SELECT * FROM sales_orders", limit=99)
    check("no truncation when it fits", res["truncated"] is False and res["total"] == 3, res)
    res = _explore.run_sql("SELECT * FROM sales_orders", limit=10 ** 9)
    check("limit clamped to MAX_LIMIT", len(res["rows"]) == 3, len(res["rows"]))


def test_guard_accepts():
    for sql in (
        "SELECT 1",
        "  select * from sales_orders where \"amount\" > 10  ",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "SELECT * FROM sales_orders; ",                       # trailing ; only
        "SELECT 1 -- a comment\n",
        "SELECT /* note */ 1",
    ):
        try:
            _explore.guard_sql(sql)
            check(f"accepts: {sql.strip()[:40]}", True)
        except Exception as e:
            check(f"accepts: {sql.strip()[:40]}", False, repr(e))
    # A literal is data, not syntax: these are legal SELECTs and rejecting
    # them would make the guard useless in a Russian 1C corpus full of
    # dashes and English words.
    out = _explore.guard_sql("SELECT * FROM t WHERE note = '-- drop table; copy'")
    check("string literal survives comment stripping",
          out == "SELECT * FROM t WHERE note = '-- drop table; copy'", out)
    # replace() is an everyday string function; a keyword blacklist that eats
    # it makes the guard unusable rather than safe.
    out = _explore.guard_sql("SELECT replace(a, 'x', 'y') FROM t")
    check("replace() is a function, not a keyword", out.startswith("SELECT replace"), out)
    out = _explore.guard_sql('SELECT "delete" FROM t')
    check("quoted identifier is not a keyword", out.startswith("SELECT"), out)


def test_guard_rejects():
    rejects("rejects INSERT", "INSERT INTO sales_orders VALUES (1)")
    rejects("rejects ATTACH", "ATTACH '/etc/passwd' AS x")
    rejects("rejects COPY", "COPY sales_orders TO '/tmp/leak.csv'")
    rejects("rejects PRAGMA", "PRAGMA database_list")
    rejects("rejects multiple statements", "SELECT 1; DROP TABLE sales_orders")
    rejects("rejects DML hidden behind a line comment",
            "SELECT 1 --\n; DELETE FROM sales_orders")
    rejects("rejects DML hidden behind a block comment",
            "SELECT 1 /* x */; UPDATE sales_orders SET a = 1")
    rejects("rejects a comment-split keyword", "SEL/**/ECT 1")
    rejects("rejects SET inside a SELECT",
            "SELECT 1 FROM (SET memory_limit='16GB')")
    rejects("rejects a filesystem reader", "SELECT * FROM read_csv('/etc/passwd')")
    rejects("rejects read_parquet", "SELECT * FROM read_parquet('h:/x.parquet')")
    rejects("rejects getenv", "SELECT getenv('HOME')")
    rejects("rejects INSTALL/LOAD of an extension", "INSTALL httpfs")
    rejects("rejects CREATE", "CREATE TABLE x AS SELECT 1")
    rejects("rejects an unterminated quote", "SELECT 'abc")
    rejects("rejects an unterminated block comment", "SELECT 1 /* abc")
    rejects("rejects the empty query", "   \n  ")
    rejects("rejects an oversized query", "SELECT " + "1," * 20000 + "1")
    # A forbidden function name written as a QUOTED identifier is still a real
    # call — DuckDB resolves `"read_csv"(...)` exactly like `read_csv(...)` —
    # so masking double-quoted text before the forbidden-word scan let every
    # one of these through. guard_sql must catch the quoted spelling too.
    rejects("rejects a quoted filesystem reader",
            "SELECT * FROM \"read_csv\"('/etc/passwd')")
    rejects("rejects a quoted glob", "SELECT * FROM \"glob\"('/*')")
    rejects("rejects a quoted getenv", "SELECT \"getenv\"('HOME')")
    rejects("rejects current_setting (configuration disclosure)",
            "SELECT current_setting('home_directory')")
    rejects("rejects duckdb_settings (configuration disclosure)",
            "SELECT * FROM duckdb_settings()")
    rejects("rejects pragma_database_list (configuration disclosure)",
            "SELECT * FROM pragma_database_list()")
    # But a column genuinely named after a DDL/DML keyword must stay
    # queryable — those keywords have no quoted-call spelling that runs them
    # as a statement, unlike the functions above.
    out = _explore.guard_sql('SELECT "delete", "set" FROM sales_orders')
    check("quoted DDL/DML keyword as a column name is still accepted",
          out.startswith("SELECT"), out)
    # run_sql must return the refusal as data, never raise into the worker.
    res = _explore.run_sql("DROP TABLE sales_orders")
    check("run_sql returns the guard error as a result field",
          res["error"] and res["rows"] == [], res)


def test_external_access_off():
    """Belt AND braces: even if the guard were bypassed, DuckDB itself must
    refuse to touch the filesystem."""
    con = _explore._conn()
    try:
        got = con.execute("SELECT current_setting('enable_external_access')").fetchone()[0]
    except Exception as e:                                   # pragma: no cover
        got = f"unreadable: {e}"
    check("enable_external_access is off", got in (False, "false", 0), got)
    blocked = False
    try:
        con.execute("SELECT * FROM read_csv_auto('/etc/hosts')").fetchall()
    except Exception:
        blocked = True
    check("DuckDB refuses a filesystem read on its own", blocked)


def test_export(tmp):
    """Roundtrip: the .xlsx bytes open again and carry the same values."""
    blob = _explore.export_xlsx('SELECT "order number", "amount", __partition '
                                'FROM sales_orders ORDER BY 1')
    check("export returns bytes", isinstance(blob, bytes) and len(blob) > 1000, len(blob))
    out = os.path.join(tmp, "out.xlsx")
    with open(out, "wb") as fh:
        fh.write(blob)
    ws = load_workbook(out).active
    rows = [[c for c in r] for r in ws.iter_rows(values_only=True)]
    # Russian on purpose: static/py/_explore.py still emits this text in Russian.
    check("export sheet name", ws.title == "Result", ws.title)
    check("export header row",
          rows[0] == ["order number", "amount", "__partition"], rows[0])
    check("export data roundtrip",
          rows[1:] == [["A-1", 100, "2026-07"], ["A-2", 200, "2026-07"],
                       ["B-1", 300, "2026-08"]], rows[1:])
    guarded = False
    try:
        _explore.export_xlsx("DELETE FROM sales_orders")
    except _explore.SQLGuardError:
        guarded = True
    check("export runs through the same guard", guarded)


def test_interrupt():
    """A runaway query is stopped, and the stop is reported in Russian rather
    than as a traceback. Needs real threads — Pyodide has none, which is why
    run_sql also enforces the deadline between fetch chunks."""
    import threading
    if not _threads_available():
        check("interrupt: skipped (no threads in this interpreter)", True)
        return
    sql = ("SELECT count(*) FROM range(9223372036854775807) t1, "
           "range(1000000) t2 WHERE t1.range = t2.range")
    before = threading.active_count()
    res = _explore.run_sql(sql, timeout_s=2)
    check("long query is stopped, not hung", res["error"] is not None, res)
    check("stop is reported",
          res["error"] and "Query stopped" in res["error"], res["error"])
    check("stopped result is flagged incomplete", res["incomplete"] is True, res)
    check("watchdog timer does not leak a thread",
          threading.active_count() <= before + 1, threading.active_count())
    # The connection survives its own interrupt — the next question must work.
    ok = _explore.run_sql("SELECT count(*) FROM sales_orders")
    check("connection usable after an interrupt", ok["rows"] == [[3]], ok)


def _threads_available() -> bool:
    import threading
    try:
        t = threading.Timer(600, lambda: None)
        t.daemon = True
        t.start()
        t.cancel()
        return True
    except Exception:
        return False


def main():
    tmp = tempfile.mkdtemp(prefix="op-explore-")
    try:
        test_union(tmp)
        test_context(tmp)
        test_reindex(tmp)
        test_time_union(tmp)
        test_semantics(tmp)
        test_type_conflict(tmp)
        test_limit(tmp)
        test_guard_accepts()
        test_guard_rejects()
        test_external_access_off()
        test_export(tmp)
        test_interrupt()
        test_cache(tmp)   # last: they reset the engine
        test_cache_portable(tmp)
    finally:
        _explore.reset()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
