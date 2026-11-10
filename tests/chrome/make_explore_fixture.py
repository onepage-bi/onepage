"""Build the /explore end-to-end fixture folder.

Five real .xlsx workbooks with the shapes the plan names:
  DataSalesOrder_07.26.xlsx / _08.26.xlsx  — TDSheet, 2-row title, header row 3
  DataRealisation_07.26.xlsx / _08.26.xlsx — TDSheet, same 2-row title
  real_forecast_SOP7.xlsx                  — Sheet1, header on row 1

The numbers are deliberate so the assertions can be exact:
  * 07.26 orders SO-001..SO-006, realisation covers SO-001..SO-004
    -> the "reconcile" recipe (orders with no realisation) = 2 rows in 07.26
    alone.
  * 08.26 repeats SO-004..SO-006 with SO-005's amount changed and adds
    SO-007 -> the "what changed" recipe 07.26 vs 08.26 = SO-007 'added', the
    07-only rows 'removed', SO-005 'changed'; see the harness for the exact
    expectation.
  * The warehouse is one of two values so a GROUP BY has more than one bucket.

The column headers are Cyrillic on purpose: the fixture mimics a Russian 1C
export, and the semantics in seed_explore.py bind to these exact headers
(lower-cased Cyrillic identifiers are part of what the harness exercises).
"""
import os
import sys
from openpyxl import Workbook

# Importers pass their own directory to build(); argv is read only when this
# file is RUN as a script. (It used to be read at import time, which made the
# harness's own flags — `--browser`, `--provider deepseek` — land as directory
# names next to the repo.)
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "explore-fixture")

TITLE = ["Exported from the accounting system", ""]

ORDERS_07 = [
    ["SO-001", "2026-07-03", "Daisy LLC",  "Northgate", 1000],
    ["SO-002", "2026-07-05", "Daisy LLC",  "Northgate", 2000],
    ["SO-003", "2026-07-09", "Vector JSC", "Southport", 3000],
    ["SO-004", "2026-07-14", "Vector JSC", "Southport", 4000],
    ["SO-005", "2026-07-20", "Daisy LLC",  "Northgate", 5000],
    ["SO-006", "2026-07-28", "Vector JSC", "Southport", 6000],
]
ORDERS_08 = [
    ["SO-004", "2026-07-14", "Vector JSC", "Southport", 4000],
    ["SO-005", "2026-07-20", "Daisy LLC",  "Northgate", 5500],   # changed
    ["SO-006", "2026-07-28", "Vector JSC", "Southport", 6000],
    ["SO-007", "2026-08-04", "Daisy LLC",  "Northgate", 7000],   # added
]
REAL_07 = [
    ["SO-001", "2026-07-04", "Northgate", 1000],
    ["SO-002", "2026-07-06", "Northgate", 2000],
    ["SO-003", "2026-07-10", "Southport", 3000],
    ["SO-004", "2026-07-15", "Southport", 4000],
]
REAL_08 = [
    ["SO-005", "2026-07-22", "Northgate", 5500],
    ["SO-006", "2026-07-30", "Southport", 6000],
]
FORECAST = [
    ["2026-07-01", "P-1", "Retail", "BV Alpha", 120],
    ["2026-08-01", "P-1", "Retail", "BV Alpha", 140],
    ["2026-08-01", "P-2", "Horeca", "BV Beta",  90],
]


def titled(path, sheet, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for line in TITLE:
        ws.append([line])
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


def plain(path, sheet, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


ORDER_HDR = ["Номер", "Дата", "Клиент", "Склад", "Сумма"]
REAL_HDR = ["Номер заказа", "Дата отгрузки", "Склад", "Сумма"]

def _write_all():
    titled(os.path.join(OUT, "DataSalesOrder_07.26.xlsx"), "TDSheet", ORDER_HDR, ORDERS_07)
    titled(os.path.join(OUT, "DataSalesOrder_08.26.xlsx"), "TDSheet", ORDER_HDR, ORDERS_08)
    titled(os.path.join(OUT, "DataRealisation_07.26.xlsx"), "TDSheet", REAL_HDR, REAL_07)
    titled(os.path.join(OUT, "DataRealisation_08.26.xlsx"), "TDSheet", REAL_HDR, REAL_08)
    plain(os.path.join(OUT, "real_forecast_SOP7.xlsx"), "Sheet1",
          ["Date", "Код продукта", "Сектор", "BV Name", "Qty"], FORECAST)


OUT = DEFAULT_OUT


def build(out=None):
    """Build the fixture folder. Importing this module writes nothing — the
    harness builds into its own scratch dir, and an import used to rewrite
    the checked-in copies under tests/chrome/explore-fixture on every run."""
    global OUT
    OUT = out or DEFAULT_OUT
    os.makedirs(OUT, exist_ok=True)
    _write_all()
    return OUT


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else None)
    for n in sorted(os.listdir(OUT)):
        print(n, os.path.getsize(os.path.join(OUT, n)))
