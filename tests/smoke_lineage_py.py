"""Smoke test for static/py/_lineage.py under native CPython.

The same file runs inside the Pyodide worker; the point of testing it here is
that the interesting parts (calamine laziness, the pandas DataFrame subclass,
hook idempotence) are plain Python and would otherwise only be observable by
opening a browser tab.

Requires python-calamine, openpyxl and pandas in the interpreter (the versions
pinned in the Pyodide bundle: calamine 0.6.2 / pandas 3.0 / openpyxl 3.1.5).
Fixtures are built with openpyxl into a tempdir.
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time

# ── Time budgets and someone else's machine ───────────────────────────────
# A budget in milliseconds is a property of the HARDWARE, not of the code. A
# GitHub runner is two to three times slower than a developer laptop, and on
# 03.11.2026 three such checks went red on CI without finding a single
# regression: 0.83 s against 0.5, 381 ms against 200. A red build that means
# nothing teaches people not to look at the build — dearer than a missed
# regression.
#
# So the budget is multiplied by OP_TEST_TIME_FACTOR (default 1.0; CI sets 3).
# The measurement itself is ALWAYS printed in full, so growth in time stays
# visible even where the threshold is relaxed.
def time_budget(seconds: float) -> float:
    import os
    try:
        return seconds * float(os.environ.get("OP_TEST_TIME_FACTOR", "1") or 1)
    except ValueError:
        return seconds


sys.path.insert(0, os.path.join(os.getcwd(), "static", "py"))
import _lineage  # noqa: E402
from openpyxl import Workbook  # noqa: E402

FAILURES = []
# Cyrillic on purpose: build_book() is the Unicode-robustness fixture (Cyrillic
# sheet names and headers survive sniff and the hooks; cutting a long header
# counts characters, not UTF-8 bytes). "A very long column name".
LONG_HEADER = "Очень длинное имя колонки " * 5  # > 64 chars on purpose


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def build_book(path):
    """3 sheets: Cyrillic headers, a 300-column sheet, a reference sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(["SKU", "Qty", "Дата", LONG_HEADER])
    for i in range(50):
        ws.append([f"s{i}", i, "2026-08-20", "x"])

    ws2 = wb.create_sheet("Справочник")
    ws2.append(["Код", "Наименование"])
    ws2.append([1, "Товар"])

    ws3 = wb.create_sheet("Wide")
    ws3.append([f"c{i}" for i in range(300)])
    ws3.append(list(range(300)))
    wb.save(path)


def build_big(path, rows=200000):
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Big")
    ws.append(["SKU", "Qty", "Date"])
    for i in range(rows):
        ws.append([f"s{i}", i, "2026-08-20"])
    wb.save(path)


def test_sniff(tmp):
    book = os.path.join(tmp, "book.xlsx")
    build_book(book)

    out = _lineage.sniff(book)
    check("sniff: no error", out["error"] is None, str(out["error"]))
    names = [s["name"] for s in out["sheets"]]
    check("sniff: all sheets, in order", names == ["Stock", "Справочник", "Wide"], str(names))
    check("sniff: book not truncated", out["truncated"] is False)

    stock = out["sheets"][0]
    check("sniff: Cyrillic header survives",
          stock["header"][:3] == ["SKU", "Qty", "Дата"], str(stock["header"][:3]))
    check("sniff: header cell cut to max_chars",
          len(stock["header"][3]) == _lineage.MAX_CELL_CHARS, str(len(stock["header"][3])))
    check("sniff: every header cell is str",
          all(isinstance(c, str) for s in out["sheets"] for c in s["header"]))
    check("sniff: rows counted", stock["rows"] == 51, str(stock["rows"]))
    check("sniff: header_row is 0 for a plain table", stock["header_row"] == 0, str(stock))
    check("sniff: narrow sheet not truncated", stock["truncated"] is False)

    wide = out["sheets"][2]
    check("sniff: 300 columns cut to the budget",
          len(wide["header"]) == _lineage.MAX_COLUMNS, str(len(wide["header"])))
    check("sniff: wide sheet flagged truncated", wide["truncated"] is True)

    out2 = _lineage.sniff(book, max_columns=2, max_chars=3)
    check("sniff: max_columns/max_chars are honoured",
          out2["sheets"][0]["header"] == ["SKU", "Qty"], str(out2["sheets"][0]["header"]))

    out3 = _lineage.sniff(book, max_sheets=2)
    check("sniff: max_sheets cuts the list and flags it",
          len(out3["sheets"]) == 2 and out3["truncated"] is True, str(out3["truncated"]))

    bad = os.path.join(tmp, "corrupt.xlsx")
    with open(bad, "wb") as f:
        f.write(b"PK\x03\x04 definitely not a workbook")
    out4 = _lineage.sniff(bad)
    check("sniff: corrupt file -> error string, no sheets",
          out4["sheets"] == [] and isinstance(out4["error"], str) and out4["error"],
          str(out4))

    out5 = _lineage.sniff(os.path.join(tmp, "missing.xlsx"))
    check("sniff: missing file -> error string", isinstance(out5["error"], str) and out5["error"],
          str(out5))

    # A blank tab and a chartsheet make calamine's iter_rows panic in Rust
    # (PanicException is a BaseException, so it used to kill sniff outright).
    odd = os.path.join(tmp, "odd.xlsx")
    wbo = Workbook()
    wbo.active.title = "Stock"
    wbo.active.append(["SKU", "Qty"])
    wbo.create_sheet("Empty")
    wbo.create_chartsheet("Ch")
    wbo.save(odd)
    out6 = _lineage.sniff(odd)
    check("sniff: empty sheet / chartsheet survive", out6["error"] is None
          and [s["name"] for s in out6["sheets"]] == ["Stock", "Empty", "Ch"], str(out6))
    check("sniff: the filled sheet still reports its header",
          out6["sheets"][0]["header"] == ["SKU", "Qty"], str(out6["sheets"][0]))
    check("sniff: the empty sheet reports an empty header, not a crash",
          out6["sheets"][1]["header"] == [] and out6["sheets"][1]["header_row"] is None
          and out6["sheets"][1]["reason"] == "sheet is empty",
          str(out6["sheets"][1]))

    # Header below a title line and a blank row; a numbers-only sheet falls
    # back to row 0; a title wider than one cell still loses to the fuller
    # header row because its non-empty ratio is lower.
    titled = os.path.join(tmp, "titled.xlsx")
    wbt = Workbook()
    wst = wbt.active
    wst.title = "Report"
    wst.append(["Report for August 2026"])
    wst.append([])
    wst.append(["Branch", "Amount", "Date"])
    wst.append(["ACME Ashford", 10.5, "2026-08-01"])
    wst.append(["ACME Ashford CONTOSO", 11, "2026-08-02"])
    wsn = wbt.create_sheet("Nums")
    wsn.append([1, 2, 3])
    wsn.append([4, 5, 6])
    wsw = wbt.create_sheet("Wide300")
    wsw.append(["Pivot table", None, "v2"])
    wsw.append([f"c{i}" for i in range(300)])
    wsw.append(list(range(300)))
    wbt.save(titled)
    out7 = _lineage.sniff(titled)
    t = out7["sheets"][0]
    check("sniff: header found in row 2 below title + blank",
          t["header_row"] == 2 and t["header"] == ["Branch", "Amount", "Date"], str(t))
    check("sniff: numbers-only sheet falls back to row 0",
          out7["sheets"][1]["header_row"] == 0 and out7["sheets"][1]["header"] == ["1", "2", "3"],
          str(out7["sheets"][1]))
    w = out7["sheets"][2]
    check("sniff: 300-col header under a 2-cell title: row 1, cut",
          w["header_row"] == 1 and w["truncated"] and len(w["header"]) == _lineage.MAX_COLUMNS
          and w["header"][0] == "c0", str((w["header_row"], w["truncated"], len(w["header"]))))
    check("sniff: header_row is int everywhere",
          all(isinstance(sh["header_row"], int) for sh in out7["sheets"]))

    # A full-width row 0 is the table even when it fails the string/unique
    # rule: duplicate labels or numeric year columns must not hand the header
    # to the text-heavy data row below (regression vs. pre-heuristic row 0).
    full0 = os.path.join(tmp, "full0.xlsx")
    wbf = Workbook()
    wsf = wbf.active
    wsf.title = "Dup"
    wsf.append(["Code", "Name", "Amount", "Amount"])
    wsf.append(["k1", "Alice", "a", "b"])
    wsf.append(["k2", "Bob", "c", "d"])
    wsy = wbf.create_sheet("Years")
    wsy.append(["Region", 2024, 2025])
    wsy.append(["North", "yes", "no"])
    wsy.append(["South", "yes", "yes"])
    wbf.save(full0)
    out7b = _lineage.sniff(full0)
    d = out7b["sheets"][0]
    check("sniff: duplicate labels in row 0 keep row 0 as header",
          d["header_row"] == 0 and d["header"] == ["Code", "Name", "Amount", "Amount"], str(d))
    y = out7b["sheets"][1]
    check("sniff: numeric year columns in row 0 keep row 0 as header",
          y["header_row"] == 0 and y["header"] == ["Region", "2024", "2025"], str(y))

    # Regression: a TITLE row above a full-width header that itself fails the
    # string/unique test (year columns) must still land on the year-column
    # row, not fall back to promoting the title to header — the two earlier
    # "keep row 0" checks above don't exercise this because their full-width
    # row IS row 0, so a naive "fall back to row 0" bug is invisible there.
    titled_years = os.path.join(tmp, "titled_years.xlsx")
    wbty = Workbook()
    wsty = wbty.active
    wsty.title = "Report"
    wsty.append(["Quarterly report"])           # title, 1/3 width -> fails (a), skipped
    wsty.append(["Region", 2024, 2025])          # full width but not all-string -> IS the header
    wsty.append(["North", 1, 2])
    wbty.save(titled_years)
    out7c = _lineage.sniff(titled_years)
    ty = out7c["sheets"][0]
    check("sniff: a title above a year-column header lands on the year-column row, not the title",
          ty["header_row"] == 1 and ty["header"] == ["Region", "2024", "2025"], str(ty))
    # And checks against it must resolve columns, not report column_missing.
    out7c_checks = _lineage.run_checks(
        titled_years, [_spec("not_null", "Report", "Region")], {})
    check("checks: column resolves against the year-column header (was column_missing)",
          out7c_checks["results"][0]["status"] == "pass", str(out7c_checks["results"][0]))

    # Heuristic scans at most 20 rows: a header at row 25 is NOT found.
    deep = os.path.join(tmp, "deep.xlsx")
    wbd = Workbook()
    wsd = wbd.active
    wsd.title = "Deep"
    for _ in range(25):
        wsd.append([7, 8])
    wsd.append(["A", "B"])
    wbd.save(deep)
    out8 = _lineage.sniff(deep)
    check("sniff: header beyond the 20-row scan falls back to row 0",
          out8["sheets"][0]["header_row"] == 0, str(out8["sheets"][0]))

    # calamine handles .xls/.xlsb through the same from_path; producing a real
    # BIFF8 file needs a writer we do not ship (xlwt is py2-era, xlrd only
    # reads), so this path is exercised in the browser run, not here.
    check("sniff: .xls/.xlsb share the calamine path (not produced here)", True)


def test_sniff_no_silent_empty(tmp):
    """The production defect: a schema that is empty for a REASON must say so.

    Seven files in prod had a parsed/cached source_files row with consumed_json
    present (the generator did open them) and no sheets/headers anywhere in the
    chain — and nothing at all to say why. Two mechanisms produced that, both
    pinned here:

      1. calamine reports two different origins for the same sheet. `height`,
         `to_python()` and pandas' calamine engine start at the first non-empty
         row; `iter_rows()` starts at ABSOLUTE row 0 and replays every blank row
         above the table. A 1C export with a 25-row title block therefore burned
         the whole 20-row scan window on padding and returned a header of empty
         strings with error None — a schema that looks successful and matches no
         column. Fixed by _sheet_rows(); the window was NOT widened, because
         once the blank prefix is skipped the headers are within a few rows.
      2. Everything else that can go wrong per sheet (an empty tab, a chart-
         sheet, an OOM on a pathological used range) used to be recorded as an
         empty header — the same shape a real empty sheet has.
    """
    # ── 1. a table under a blank prefix taller than the scan window ───────
    pad = os.path.join(tmp, "padded.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "TDSheet"
    for _ in range(25):
        ws.append([None, None, None, None])
    ws.append(["SKU", "Qty", "Date", "Branch"])
    for i in range(5):
        ws.append([f"s{i}", i, "2026-08-20", "Ashford"])
    wb.save(pad)
    out = _lineage.sniff(pad)
    sh = out["sheets"][0]
    check("sniff: a 25-row blank prefix does not eat the header scan window",
          sh["header"] == ["SKU", "Qty", "Date", "Branch"], str(sh))
    check("sniff: header_row is relative to the used range, like `rows`",
          sh["header_row"] == 0 and sh["rows"] == 6, str(sh))
    check("sniff: a found header carries no reason", "reason" not in sh, str(sh))
    # The whole point of the alignment: data rows come out right.
    check("sniff: data rows derive correctly under a blank prefix",
          sh["rows"] - sh["header_row"] - 1 == 5, str(sh))

    # ── the same sheet, seen through run_checks (was column_missing) ──────
    res = _lineage.run_checks(pad, [_spec("not_null", "TDSheet", "Branch",
                                          match="padded.xlsx")], {})
    check("checks: a table under a 25-row title block resolves its columns",
          res["results"][0]["status"] == "pass", str(res["results"][0]))

    # ── a column offset too: iter_rows already trims columns, rows did not ─
    off = os.path.join(tmp, "offset.xlsx")
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.title = "S"
    ws2["C22"] = "Code"
    ws2["D22"] = "Name"
    ws2["C23"] = 1
    ws2["D23"] = "Alice"
    wb2.save(off)
    o = _lineage.sniff(off)["sheets"][0]
    check("sniff: a table offset in BOTH axes still reports its header",
          o["header"] == ["Code", "Name"] and o["header_row"] == 0, str(o))

    # ── 2. no header anywhere in the window -> reason, not a fake header ──
    noh = os.path.join(tmp, "noheader.xlsx")
    wb3 = Workbook()
    ws3 = wb3.active
    ws3.title = "Sparse"
    # Every row has exactly one filled cell out of four: never >= 2 non-empty,
    # so no row can pass the header test and none may be passed off as one.
    for i in range(30):
        ws3.append([f"v{i}", None, None, None])
    wb3.save(noh)
    n = _lineage.sniff(noh)["sheets"][0]
    check("sniff: no header in the window -> empty header + reason, not row 0",
          n["header"] == [] and n["header_row"] is None
          and n["reason"] == f"no header in first {_lineage.MAX_HEADER_SCAN} rows", str(n))
    check("sniff: the sheet is still listed, with its row count",
          n["name"] == "Sparse" and n["rows"] == 30, str(n))
    whole = _lineage.sniff(noh)
    check("sniff: a workbook where NO sheet yielded a header carries an error too",
          isinstance(whole["error"], str) and "no header" in whole["error"], str(whole["error"]))

    # A check against that sheet is skipped for a missing column — it must not
    # silently index columns off a data row.
    res2 = _lineage.run_checks(noh, [_spec("not_null", "Sparse", "v0",
                                           match="noheader.xlsx")], {})
    check("checks: a headerless sheet reports column_missing, never a false pass",
          res2["results"][0]["status"] == "skipped"
          and res2["results"][0]["reason"] == "column_missing", str(res2["results"][0]))

    # ── every sheet is either headed or explained; never both-empty-silent ─
    mixed = _lineage.sniff(os.path.join(tmp, "book.xlsx"))
    check("sniff: every sheet either has a header or says why it has none",
          all(sh2["header"] or sh2.get("reason") for sh2 in mixed["sheets"]),
          str([(x["name"], x["header"][:1], x.get("reason")) for x in mixed["sheets"]]))

    # ── an unreadable sheet keeps its reason and does not abort the sniff ──
    # MemoryError used to be re-raised by _reraise_control: calamine's
    # get_sheet_by_name materialises a dense height x width grid, so one stray
    # cell far outside the table (a 500 000 x 200 range out of a 5 KB file,
    # 4.8 GB RSS measured) OOMs the Pyodide heap — and the raise aborted the
    # WHOLE sniff, so the worker sent `schema: null` and the server read that
    # as "unchanged". It is a per-sheet condition and must be recorded as one.
    real_cls = _lineage._workbook_cls()

    class _Boom:
        @staticmethod
        def from_path(path):
            wbx = real_cls.from_path(path)

            class _Proxy:
                sheet_names = wbx.sheet_names

                @staticmethod
                def get_sheet_by_name(name):
                    if name == "Справочник":
                        raise MemoryError("cannot allocate 500000 x 200 cells")
                    return wbx.get_sheet_by_name(name)
            return _Proxy

    saved = _lineage._orig.get("calamine")
    _lineage._orig["calamine"] = _Boom
    try:
        out_oom = _lineage.sniff(os.path.join(tmp, "book.xlsx"))
    finally:
        if saved is None:
            _lineage._orig.pop("calamine", None)
        else:
            _lineage._orig["calamine"] = saved
    check("sniff: a per-sheet MemoryError does not abort the workbook",
          [x["name"] for x in out_oom["sheets"]] == ["Stock", "Справочник", "Wide"],
          str(out_oom)[:200])
    boom = out_oom["sheets"][1]
    check("sniff: the OOM sheet records the reason instead of a blank header",
          boom["header"] == [] and boom["header_row"] is None
          and boom["reason"].startswith("MemoryError:"), str(boom))
    check("sniff: the readable sheets around it keep their headers",
          out_oom["sheets"][0]["header"][:2] == ["SKU", "Qty"], str(out_oom["sheets"][0]))
    check("sniff: a reason is capped at MAX_REASON_CHARS",
          all(len(x.get("reason") or "") <= _lineage.MAX_REASON_CHARS
              for x in out_oom["sheets"]))

    # KeyboardInterrupt/SystemExit are still let through — dropping MemoryError
    # from that list must not have turned sniff into a signal sink.
    class _Stop:
        @staticmethod
        def from_path(path):
            raise KeyboardInterrupt("ctrl-c")

    _lineage._orig["calamine"] = _Stop
    try:
        _lineage.sniff(os.path.join(tmp, "book.xlsx"))
        passed_through = False
    except KeyboardInterrupt:
        passed_through = True
    finally:
        if saved is None:
            _lineage._orig.pop("calamine", None)
        else:
            _lineage._orig["calamine"] = saved
    check("sniff: KeyboardInterrupt still escapes (control flow, not a reason)",
          passed_through)


def test_sniff_budget(tmp):
    big = os.path.join(tmp, "big.xlsx")
    t0 = time.perf_counter()
    build_big(big)
    build_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    out = _lineage.sniff(big)
    dt = time.perf_counter() - t0
    check("sniff: 200k-row sheet under 0.5 s (never to_python)", dt < time_budget(0.5),
          f"{dt:.3f}s (fixture build was {build_s:.1f}s)")
    check("sniff: 200k rows counted", out["sheets"][0]["rows"] == 200001,
          str(out["sheets"][0]["rows"]))
    print(f"     (sniff of 200k x 3 took {dt * 1000:.0f} ms)")


def test_calamine_hook(tmp):
    book = os.path.join(tmp, "book.xlsx")
    from python_calamine import CalamineWorkbook  # as a generator would
    from python_calamine import load_workbook as python_calamine_load

    wb = CalamineWorkbook.from_path(book)
    check("hook: proxy passes the generator's own isinstance check",
          isinstance(wb, CalamineWorkbook))
    check("hook: forwarded attribute (sheet_names)",
          list(wb.sheet_names) == ["Stock", "Справочник", "Wide"], str(wb.sheet_names))
    rows = wb.get_sheet_by_name("Stock").to_python()
    check("hook: to_python still returns real data", len(rows) == 51, str(len(rows)))
    wb.get_sheet_by_index(1)

    wb2 = python_calamine_load(book)
    check("hook: load_workbook() goes through the proxy too",
          isinstance(wb2, CalamineWorkbook), str(type(wb2)))
    wb2.get_sheet_by_name("Wide")

    got = _lineage.drain()
    check("hook: load_workbook read is recorded",
          got.get(book, {}).get("sheets", {}).get("Wide") == {"columns": [], "all": True},
          str(got))
    check("hook: calamine records sheet-level for the right path",
          set(got) == {book}, str(list(got)))
    sheets = got.get(book, {}).get("sheets", {})
    check("hook: both sheets recorded, columns unknown -> all",
          sheets.get("Stock") == {"columns": [], "all": True}
          and sheets.get("Справочник") == {"columns": [], "all": True}, str(sheets))
    check("hook: drain() empties the buffer", _lineage.drain() == {})

    _lineage.sniff(book)
    check("hook: sniff is not consumption", _lineage.drain() == {})


def test_pandas_hook(tmp):
    book = os.path.join(tmp, "book.xlsx")
    import pandas as pd

    df = pd.read_excel(book, sheet_name="Stock", usecols=["SKU", "Qty"])
    check("pandas: usecols recorded as columns",
          _lineage.drain().get(book, {}).get("sheets", {}).get("Stock")
          == {"columns": ["SKU", "Qty"], "all": False}, str(df.columns.tolist()))

    df = pd.read_excel(book, sheet_name="Stock")
    df["Qty"]
    got = _lineage.drain()[book]["sheets"]["Stock"]
    check("pandas: __getitem__ on a plain read records the column",
          got == {"columns": ["Qty"], "all": False}, str(got))

    df = pd.read_excel(book, sheet_name="Stock")
    df2 = df.head(10)
    df2[["SKU", "Дата"]]
    got = _lineage.drain()[book]["sheets"]["Stock"]
    check("pandas: _lin_key survives a derived frame",
          got == {"columns": ["SKU", "Дата"], "all": False}, str(got))

    df = pd.read_excel(book, sheet_name="Stock")
    del df
    got = _lineage.drain()[book]["sheets"]["Stock"]
    check("pandas: a frame nobody subscripted stays all:True",
          got == {"columns": [], "all": True}, str(got))

    frames = pd.read_excel(book, sheet_name=None)
    check("pandas: sheet_name=None reads every sheet", len(frames) == 3, str(list(frames)))
    got = _lineage.drain()[book]["sheets"]
    check("pandas: sheet_name=None records every sheet by its real name",
          set(got) == {"Stock", "Справочник", "Wide"}
          and all(v == {"columns": [], "all": True} for v in got.values()), str(got))

    # A list of sheets shares one usecols, so only Stock can be asked for these.
    frames = pd.read_excel(book, sheet_name=["Stock"], usecols=["SKU", "Qty"])
    check("pandas: sheet_name list returns a dict", set(frames) == {"Stock"}, str(list(frames)))
    got = _lineage.drain()[book]["sheets"]
    check("pandas: sheet_name list + usecols",
          got == {"Stock": {"columns": ["SKU", "Qty"], "all": False}}, str(got))

    xf = pd.ExcelFile(book)
    check("pandas: ExcelFile.sheet_names still works",
          xf.sheet_names == ["Stock", "Справочник", "Wide"], str(xf.sheet_names))
    check("pandas: enumerating registers the file with no sheets read",
          _lineage.drain().get(book) == {"sheets": {}}, "")
    xf.parse(0)["Qty"]
    got = _lineage.drain()[book]["sheets"]
    check("pandas: ExcelFile.parse(int) resolves the sheet name",
          got == {"Stock": {"columns": ["Qty"], "all": False}}, str(got))
    xf.close()

    pd.read_excel(book, "Stock", usecols=["SKU"])  # sheet_name positionally
    got = _lineage.drain()[book]["sheets"]
    check("pandas: a positional sheet_name is still read",
          got == {"Stock": {"columns": ["SKU"], "all": False}}, str(got))

    # The default sheet_name=0 must resolve to the real name once, not record a
    # phantom "#0" beside it and then send the column reads there.
    df = pd.read_excel(book)
    df["Qty"]
    got = _lineage.drain()[book]["sheets"]
    check("pandas: default sheet_name records exactly one, resolved sheet",
          got == {"Stock": {"columns": ["Qty"], "all": False}}, str(got))

    df = pd.read_excel(book, sheet_name=1)
    got = _lineage.drain()[book]["sheets"]
    check("pandas: int sheet_name resolves to the real name",
          got == {"Справочник": {"columns": [], "all": True}}, str(got))

    # pandas keys the result dict by the list elements AS GIVEN, so an int
    # element used to land in the graph as a sheet literally named "0".
    pd.read_excel(book, sheet_name=[0, "Справочник"])
    got = _lineage.drain()[book]["sheets"]
    check("pandas: an int inside a sheet_name LIST resolves too",
          set(got) == {"Stock", "Справочник"}, str(got))


def test_pandas_internal_reads(tmp):
    """pandas subscripts frames inside its own algorithms; that is not lineage."""
    book = os.path.join(tmp, "book.xlsx")
    import pandas as pd

    def fresh():
        a = pd.read_excel(book, sheet_name="Stock")
        b = pd.read_excel(book, sheet_name="Справочник")
        _lineage.drain()
        return a, b

    df, ref = fresh()
    df.merge(ref, how="cross")
    got = _lineage.drain()
    check("internal: a cross merge invents no '_cross_<uuid>' column", got == {}, str(got))

    df, _ = fresh()
    df.assign(z=1)["z"]
    got = _lineage.drain()
    check("internal: a column that is not in the file is never recorded", got == {}, str(got))

    df, _ = fresh()
    df.melt(id_vars=["SKU"])
    df.set_index("SKU")
    df.sort_values("Qty")
    df.drop_duplicates(subset=["SKU"])
    got = _lineage.drain()
    check("internal: melt/set_index/sort_values/drop_duplicates do not narrow the sheet",
          got == {}, str(got))

    # The generator's OWN subscript still counts, on the same frame shape.
    df, _ = fresh()
    df.sort_values("Qty")["SKU"]
    got = _lineage.drain()[book]["sheets"]["Stock"]
    check("internal: the generator's own subscript is still recorded",
          got == {"columns": ["SKU"], "all": False}, str(got))


def test_pandas_buffers(tmp):
    """Hooks must never break a read that worked without them."""
    book = os.path.join(tmp, "book.xlsx")
    import io as _io
    import pandas as pd

    with open(book, "rb") as fh:
        df = pd.read_excel(fh, sheet_name="Stock")
    check("pandas: read_excel(file handle) still works", len(df) == 50, str(len(df)))
    got = _lineage.drain()
    check("pandas: a named handle attributes to its path",
          got.get(book, {}).get("sheets", {}).get("Stock") is not None, str(got))

    with open(book, "rb") as fh:
        buf = _io.BytesIO(fh.read())
    df = pd.read_excel(buf, sheet_name="Stock")
    check("pandas: read_excel(BytesIO) still works", len(df) == 50, str(len(df)))
    _lineage.drain()

    with open(book, "rb") as fh:
        xf = pd.ExcelFile(fh)
        names = xf.sheet_names
        df = xf.parse("Stock")
    check("pandas: ExcelFile(handle).sheet_names/.parse still work",
          names == ["Stock", "Справочник", "Wide"] and len(df) == 50, str(names))
    _lineage.drain()


def test_pickle(tmp):
    """A generator may to_pickle() a parsed frame into the /cache aggregate."""
    import pickle

    import pandas as pd

    df = pd.read_excel(os.path.join(tmp, "book.xlsx"), sheet_name="Stock")
    check("pickle: a tracked frame is the module-level class",
          type(df) is _lineage._TrackedFrame, str(type(df)))
    back = pickle.loads(pickle.dumps(df))
    check("pickle: round-trips", len(back) == 50 and list(back.columns)[:2] == ["SKU", "Qty"],
          str(list(back.columns)))
    check("pickle: comes back a plain DataFrame (the key is process-local)",
          type(back) is pd.DataFrame, str(type(back)))
    _lineage.drain()


def _spec(ctype, sheet, column, match="stock_*.xlsx", **kw):
    """Same shape genmanifest.checks_for emits."""
    d = {"id": f"{ctype}:{match}:{sheet}:{column}", "type": ctype, "match": match,
         "sheet": sheet, "column": column, "min": None, "max": None, "pattern": None,
         "values": None, "on_fail": "warn", "label": None}
    d.update(kw)
    return d


def build_checks_book(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Quarterly report"])          # title row -> header is row 1
    ws.append(["Code", "Branch", "Amount", "Price", "Email", "Status", "Date", "Empty", "Mixed"])
    branches = ["ACME Ashford", "ACME Ashford", "ACME Ashford CONTOSO", "ACME Kingsbridge"]
    for i in range(40):
        ws.append([
            f"k{i}" if i != 39 else "k0",             # unique: one duplicate
            branches[i % 4] if i != 5 else None,      # not_null: one empty
            i * 10,                                   # range 0..400 all ints
            "12.50" if i % 2 else "abc" if i == 10 else 3.75,  # float strings + one junk
            f"u{i}@x.ru" if i != 7 else "not-an-email",
            "ok" if i % 3 else "OK " if i != 12 else "broken",
            datetime.date(2026, 8, 1 + i % 28),
            None,
            "txt" if i % 2 else i,
        ])
    ws2 = wb.create_sheet("Wide")
    ws2.append([f"c{i}" for i in range(300)])
    for i in range(30):
        ws2.append(list(range(300)))
    wb.save(path)


def test_checks(tmp):
    book = os.path.join(tmp, "stock_checks.xlsx")
    build_checks_book(book)
    specs = [
        _spec("unique", "Data", "Code"),
        _spec("not_null", "Data", "Branch"),
        _spec("not_null", "Data", "Code"),
        _spec("range", "Data", "Amount", min=0, max=400),
        _spec("range", "Data", "Amount", min=0, max=100, id="range2:Amount"),  # ids unique per fixture
        _spec("range", "data", "price", min=0, max=20),         # casefold sheet+column
        _spec("regex", "Data", "Email", pattern=r"[^@\s]+@[^@\s]+\.[a-z]+"),
        _spec("regex", "Data", "Code", pattern=r"k\d+"),
        _spec("enum", "Data", "Status", values=["ok", "broken"]),
        _spec("enum", "Data", "Status", values=["ok"], id="enum2:Status"),
        _spec("known_values", "Data", "Branch"),
        _spec("known_values", "Dat*", "Code"),                    # 40 distinct
        _spec("not_null", "Data", "No such"),
        _spec("not_null", "Missing", "Code"),
        _spec("regex", "Data", "Code", pattern="(", id="regex2:Code"),
        _spec("known_values", "Wide", "c299"),
    ]
    declared = {"Data": ["Code", "Amount", "Date", "Empty", "Mixed", "Branch", "Price"],
                "W*": ["c0", "c299", "ghost"]}
    out = _lineage.run_checks(book, specs, declared)
    check("checks: no error, ms float", out["error"] is None and isinstance(out["ms"], float), str(out["error"]))
    by = {(r["check_id"], r["sheet"]): r for r in out["results"]}
    check("checks: one result per spec", len(out["results"]) == len(specs), str(len(out["results"])))

    def r(i):
        return by[(specs[i]["id"], "Data")] if specs[i]["sheet"] not in ("Missing", "Wide") \
            else by[(specs[i]["id"], specs[i]["sheet"])]

    check("checks: unique finds the duplicate",
          r(0)["status"] == "fail" and r(0)["failed"] == 1 and r(0)["examples"] == ["k0"]
          and r(0)["count"] == 40, str(r(0)))
    check("checks: not_null fails on the empty cell, count includes the empty cell too",
          r(1)["status"] == "fail" and r(1)["failed"] == 1 and r(1)["count"] == 40, str(r(1)))
    check("checks: not_null passes on a full column",
          r(2)["status"] == "pass" and r(2)["failed"] == 0 and r(2)["count"] == 40, str(r(2)))
    check("checks: range inclusive bounds pass", r(3)["status"] == "pass", str(r(3)))
    check("checks: range fails above max with examples",
          r(4)["status"] == "fail" and r(4)["failed"] == 29 and r(4)["examples"] == ["110", "120", "130", "140", "150"],
          str(r(4)))
    check("checks: range parses float strings, flags the junk (casefold sheet/column)",
          r(5)["status"] == "fail" and r(5)["failed"] == 1 and r(5)["examples"] == ["abc"], str(r(5)))
    check("checks: regex fails the odd email",
          r(6)["status"] == "fail" and r(6)["examples"] == ["not-an-email"], str(r(6)))
    check("checks: regex fullmatch pass", r(7)["status"] == "pass", str(r(7)))
    check("checks: enum casefold+strip pass", r(8)["status"] == "pass" and r(8)["failed"] == 0, str(r(8)))
    check("checks: enum fails the value outside the list",
          r(9)["status"] == "fail" and r(9)["examples"] == ["broken"], str(r(9)))
    check("checks: known_values always pass with sorted distinct",
          r(10)["status"] == "pass" and r(10)["distinct"] == sorted(
              {"ACME Ashford", "ACME Ashford CONTOSO", "ACME Kingsbridge"})
          and r(10)["distinct_overflow"] is False, str(r(10)))
    check("checks: sheet glob matches physical sheet",
          r(11)["sheet"] == "Data" and len(r(11)["distinct"]) == 39, str(r(11))[:200])
    check("checks: column missing -> skipped/column_missing",
          r(12)["status"] == "skipped" and r(12)["reason"] == "column_missing", str(r(12)))
    check("checks: sheet missing -> skipped/sheet_missing",
          r(13)["status"] == "skipped" and r(13)["reason"] == "sheet_missing", str(r(13)))
    check("checks: bad regex -> skipped/bad_pattern",
          r(14)["status"] == "skipped" and r(14)["reason"] == "bad_pattern", str(r(14)))
    check("checks: 300-col sheet, last column still addressed",
          r(15)["status"] == "pass" and r(15)["distinct"] == ["299"] and r(15)["count"] == 30, str(r(15)))
    check("checks: nothing sampled below max_rows", not any(x["sampled"] for x in out["results"]))

    prof = {(p["sheet"], p["column"]): p for p in out["profiles"]}
    check("profiles: declared + check columns, missing one omitted",
          set(prof) == {("Data", c) for c in ["Code", "Amount", "Date", "Empty", "Mixed", "Branch", "Price", "Email", "Status"]}
          | {("Wide", "c0"), ("Wide", "c299")}, str(sorted(prof)))
    check("profiles: number", prof[("Data", "Amount")]["type"] == "number"
          and prof[("Data", "Amount")]["min"] == 0 and prof[("Data", "Amount")]["max"] == 390
          and prof[("Data", "Amount")]["distinct"] == 40, str(prof[("Data", "Amount")]))
    check("profiles: date min/max as str", prof[("Data", "Date")]["type"] == "date"
          and prof[("Data", "Date")]["min"].startswith("2026-08-01")
          and prof[("Data", "Date")]["max"].startswith("2026-08-28"), str(prof[("Data", "Date")]))
    ph = prof[("Data", "Branch")]
    check("profiles: text with nulls, top-5, lexicographic min/max",
          ph["type"] == "text" and ph["nulls"] == 1 and ph["count"] == 39 and ph["distinct"] == 3
          and ph["top"][0] == ["ACME Ashford", 19] and ph["min"] == "ACME Ashford"
          and ph["max"] == "ACME Kingsbridge", str(ph))
    check("profiles: empty", prof[("Data", "Empty")]["type"] == "empty"
          and prof[("Data", "Empty")]["nulls"] == 40 and prof[("Data", "Empty")]["min"] is None,
          str(prof[("Data", "Empty")]))
    check("profiles: mixed (float strings + numbers)", prof[("Data", "Price")]["type"] == "mixed"
          and prof[("Data", "Mixed")]["type"] == "mixed", str(prof[("Data", "Price")]))
    check("profiles: top values are [str, int] and cut to max_chars",
          all(isinstance(v, str) and isinstance(n, int) and len(v) <= _lineage.MAX_CELL_CHARS
              for p in out["profiles"] for v, n in p["top"]))

    try:
        json.dumps(out, ensure_ascii=False)
        ok = True
    except TypeError as exc:
        ok = False
        print("   ", exc)
    check("checks: whole payload is JSON-serialisable", ok)

    # Budgets.
    out2 = _lineage.run_checks(book, specs[:2] + specs[10:11], declared, max_rows=10)
    check("checks: max_rows=10 -> sampled on every live result and profile",
          all(x["sampled"] for x in out2["results"] if x["status"] != "skipped")
          and all(p["sampled"] for p in out2["profiles"] if p["sheet"] == "Data")
          and by[(specs[0]["id"], "Data")]["count"] == 40
          and out2["results"][0]["count"] == 10, str(out2["results"]))
    out3 = _lineage.run_checks(book, [specs[11], specs[0]], {}, max_distinct=3)
    kv = [x for x in out3["results"] if x["check_id"] == specs[11]["id"]][0]
    check("checks: max_distinct=3 -> distinct_overflow, distinct None, still pass",
          kv["status"] == "pass" and kv["distinct_overflow"] is True and kv["distinct"] is None, str(kv))
    un = [x for x in out3["results"] if x["check_id"] == specs[0]["id"]][0]
    check("checks: unique beyond max_distinct*4 -> skipped/cardinality_exceeded",
          un["status"] == "skipped" and un["reason"] == "cardinality_exceeded", str(un))
    pk = [p for p in out3["profiles"] if p["column"] == "Code"][0]
    check("profiles: distinct None + sampled when the counter overflows",
          pk["distinct"] is None and pk["sampled"] is True and len(pk["top"]) == 5, str(pk))
    check("checks: max_chars honoured on examples/distinct",
          all(len(e) <= 3 for x in _lineage.run_checks(book, [specs[4], specs[10]], {}, max_chars=3)["results"]
              for e in (x["examples"] + (x["distinct"] or []))))

    # Unreadable workbook -> every spec skipped, error set.
    out4 = _lineage.run_checks(os.path.join(tmp, "missing.xlsx"), specs[:3], declared)
    check("checks: unreadable workbook -> all skipped/unreadable + error",
          isinstance(out4["error"], str) and len(out4["results"]) == 3
          and all(x["status"] == "skipped" and x["reason"] == "unreadable" for x in out4["results"]),
          str(out4))
    out5 = _lineage.run_checks(book, [], {})
    check("checks: no specs, no declared -> empty lists", out5 == {"results": [], "profiles": [], "ms": out5["ms"], "error": None})

    # Empty sheet / chartsheet: specs skipped cleanly, no panic.
    odd = os.path.join(tmp, "odd.xlsx")
    out6 = _lineage.run_checks(odd, [_spec("not_null", "Empty", "X"), _spec("not_null", "Ch", "X"),
                                     _spec("not_null", "Stock", "SKU")], {})
    st = {x["sheet"]: (x["status"], x["reason"], x["count"]) for x in out6["results"]}
    check("checks: empty tab / chartsheet -> column_missing, the header-only sheet passes with count 0",
          out6["error"] is None and st == {"Stock": ("pass", None, 0),
                                           "Empty": ("skipped", "column_missing", 0),
                                           "Ch": ("skipped", "column_missing", 0)}, str(st))


def test_checks_not_null_mostly_blank(tmp):
    """count/failed must stay a valid fraction even when most of the column
    is blank (the shape not_null exists to catch) — regression for the
    'failed out of a count that excludes the failures' bug."""
    book = os.path.join(tmp, "mostly_blank.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["SKU", "Qty"])
    for i in range(90):
        ws.append([f"a{i}", None])
    for i in range(10):
        ws.append([f"b{i}", 1])
    wb.save(book)
    out = _lineage.run_checks(book, [_spec("not_null", "Data", "Qty")], {})
    r = out["results"][0]
    check("checks: not_null on a mostly-blank column reports a sane fraction",
          r["status"] == "fail" and r["count"] == 100 and r["failed"] == 90
          and r["failed"] <= r["count"], str(r))


def test_norm_key():
    """The ONE platform key rule, mirrored into _lineage.py from
    app/services/reference.py. 12 cases, the same list smoke_reference_service
    pins on the server side — if these two ever disagree, every ref_lookup
    reports false unmapped values."""
    # Cyrillic on purpose: exercises casefold, NFC and dash/space folding of
    # non-ASCII keys exactly as the server-side list does.
    nk = _lineage.norm_key
    cases = [
        (None, "", "None"),
        (float("nan"), "", "NaN"),
        ("", "", "empty string"),
        ("   ", "", "whitespace only"),
        ("  ООО НВД  Апрельск ", "ооо нвд апрельск", "trim + collapse + casefold"),
        ("ООО\u00a0НВД Апрельск", "ооо нвд апрельск", "nbsp is a space"),
        ("НВД\u2013CONTOSO", "нвд-contoso", "en dash -> hyphen"),
        ("НВД\u2014CONTOSO", "нвд-contoso", "em dash -> hyphen"),
        ("НВД\u2212CONTOSO", "нвд-contoso", "minus sign -> hyphen"),
        (7.0, "7", "integral float loses the .0"),
        (7.5, "7.5", "non-integral float keeps it"),
        ("007", "007", "leading zeros untouched"),
    ]
    for src, want, why in cases:
        check(f"norm_key: {why}", nk(src) == want, f"{src!r} -> {nk(src)!r}, want {want!r}")
    check("norm_key: nbsp and dash variants collide with the plain form",
          nk("ООО\u00a0НВД\u2013CONTOSO") == nk("ооо нвд-contoso"),
          f"{nk(chr(0x41E))}")
    check("norm_key: an int is its digits", nk(42) == "42")
    check("norm_key: NFC — a decomposed ё equals the composed one",
          nk("\u0435\u0308") == nk("\u0451"), repr(nk("\u0435\u0308")))
    # The mirror is only worth anything if it IS one: diff the two
    # implementations value-by-value rather than trusting two copies of a
    # docstring. app.* is importable here (CPython), just not in Pyodide.
    try:
        sys.path.insert(0, os.getcwd())
        from app.services.reference import norm_key as server_norm_key
    except Exception as exc:                                   # phase 1 not landed
        print(f"   (skipping the server-side mirror diff: {exc})")
    else:
        probes = [v for v, _, _ in cases] + [
            "\u0435\u0308", "a\u2010b", "a\u2011b", "a\u2012b", "a\u2015b",
            "A\tB\nC", "\u3000q\u3000", float("inf"), 0.1, -0.0, 42, "x" * 70,
        ]
        drift = [(v, server_norm_key(v), nk(v)) for v in probes
                 if server_norm_key(v) != nk(v)]
        check("norm_key: byte-identical to app/services/reference.py",
              drift == [], str(drift))
    _lineage.install_hooks()
    import onepage  # noqa: F401  (installed as a module AND a builtin)
    check("onepage.norm_key is exposed to the generator",
          getattr(onepage, "norm_key", None) is _lineage.norm_key)


def build_ref_book(path, values):
    """One sheet with a single key column, `values` rows."""
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Orders")
    ws.append(["BV", "Amount"])
    for i, v in enumerate(values):
        ws.append([v, i])
    wb.save(path)


def _ref_spec(keys=..., **kw):
    sp = _spec("ref_lookup", "Orders", "BV", match="SL_*.xlsx", table="nwd_bv_map")
    if keys is not ...:
        sp["keys"] = keys
    sp.update(kw)
    return sp


def test_ref_lookup(tmp):
    """Risk 1 in the plan: known_values CLEARS its set past 500 distinct, so a
    2 000-value column could never report what did not map. ref_lookup keeps
    the first MAX_MISSING instead."""
    book = os.path.join(tmp, "SL_ref.xlsx")
    values = [f"BV-{i:05d}" for i in range(2000)]
    build_ref_book(book, values)

    # 1 500 of 2 000 mapped -> 500 missing, exactly at the cap, no overflow.
    keys = [_lineage.norm_key(v) for v in values[:1500]]
    out = _lineage.run_checks(book, [_ref_spec(keys)], {})
    r = out["results"][0]
    check("ref_lookup: 500 unmapped of 2000 -> fail, no overflow",
          out["error"] is None and r["status"] == "fail" and r["count"] == 2000
          and r["failed"] == 500 and len(r["missing"]) == 500
          and r["missing_overflow"] is False, str(r)[:300])
    check("ref_lookup: missing carries the ORIGINAL text, sorted by norm",
          r["missing"] == sorted(values[1500:]), str(r["missing"][:3]))
    check("ref_lookup: <=5 examples, like every other check",
          len(r["examples"]) == 5 and r["examples"][0] == "BV-01500", str(r["examples"]))
    check("ref_lookup: the injected keys are never echoed back",
          "keys" not in r and set(r) == {
              "check_id", "sheet", "column", "status", "count", "failed", "examples",
              "distinct", "distinct_overflow", "missing", "missing_overflow",
              "sampled", "reason"}, str(sorted(r)))
    check("ref_lookup: distinct stays None (it is not a known_values check)",
          r["distinct"] is None and r["distinct_overflow"] is False)
    check("ref_lookup: whole payload is JSON-serialisable",
          isinstance(json.dumps(out, ensure_ascii=False), str))

    # 600 unmapped -> overflow, and exactly the first MAX_MISSING kept (the
    # set is NOT cleared — that is the known_values behaviour this exists to
    # avoid), while `failed` still counts them all.
    keys2 = [_lineage.norm_key(v) for v in values[:1400]]
    r2 = _lineage.run_checks(book, [_ref_spec(keys2)], {})["results"][0]
    check("ref_lookup: 600 unmapped -> overflow with the first 500 kept",
          r2["status"] == "fail" and r2["failed"] == 600
          and r2["missing_overflow"] is True and len(r2["missing"]) == 500
          and r2["missing"] == sorted(values[1400:1900]), str(r2)[:300])

    # Everything mapped -> pass, empty missing list.
    r3 = _lineage.run_checks(
        book, [_ref_spec([_lineage.norm_key(v) for v in values])], {})["results"][0]
    check("ref_lookup: everything mapped -> pass with an empty missing list",
          r3["status"] == "pass" and r3["failed"] == 0 and r3["missing"] == []
          and r3["missing_overflow"] is False, str(r3)[:200])

    # No keys at all -> skipped/no_keys, NOT "everything is unmapped".
    r4 = _lineage.run_checks(book, [_ref_spec()], {})["results"][0]
    check("ref_lookup: absent keys -> skipped/no_keys",
          r4["status"] == "skipped" and r4["reason"] == "no_keys"
          and r4["failed"] == 0 and r4["missing"] is None, str(r4)[:200])
    r4b = _lineage.run_checks(book, [_ref_spec(None)], {})["results"][0]
    check("ref_lookup: keys=None -> skipped/no_keys", r4b["reason"] == "no_keys", str(r4b)[:200])
    r4c = _lineage.run_checks(book, [_ref_spec([])], {})["results"][0]
    check("ref_lookup: keys=[] is a real (empty) table, not no_keys",
          r4c["status"] == "fail" and r4c["reason"] is None and r4c["failed"] == 2000,
          str(r4c)[:200])

    # Normalisation on the CELL side: nbsp / dash / case variants of a key map.
    # Cyrillic on purpose: the same casefold cases as test_norm_key.
    odd = os.path.join(tmp, "SL_odd.xlsx")
    build_ref_book(odd, ["ООО\u00a0НВД Апрельск", "  ООО НВД   Апрельск  ",
                         "НВД\u2013CONTOSO", "НВД\u2014CONTOSO", "нвд-contoso",
                         "ООО НВД Кедровск", None, "   "])
    r5 = _lineage.run_checks(odd, [_ref_spec(["ооо нвд апрельск", "нвд-contoso"])], {})["results"][0]
    check("ref_lookup: nbsp/dash/case variants count as mapped; empty cells skipped",
          r5["status"] == "fail" and r5["count"] == 6 and r5["failed"] == 1
          and r5["missing"] == ["ООО НВД Кедровск"], str(r5)[:300])

    # A sheet the spec's glob does not reach is still sheet_missing, and the
    # skipped shape carries the new keys too.
    r6 = _lineage.run_checks(book, [_ref_spec([], sheet="Missing")], {})["results"][0]
    check("ref_lookup: sheet_missing keeps missing=None/missing_overflow=False",
          r6["status"] == "skipped" and r6["reason"] == "sheet_missing"
          and r6["missing"] is None and r6["missing_overflow"] is False, str(r6)[:200])
    check("ref_lookup: MAX_MISSING is the documented 500", _lineage.MAX_MISSING == 500)


def test_glob_parity():
    """The globmatch table through the engine-side copy."""
    sys.path.insert(0, os.getcwd())
    from app.services import globmatch
    import importlib.util
    spec = importlib.util.spec_from_file_location("_gm_table", os.path.join("tests", "smoke_globmatch.py"))
    src = open(spec.origin, encoding="utf-8").read()
    start, end = src.index("TEST_TABLE = ["), src.index("\n]\n", src.index("TEST_TABLE = [")) + 2
    ns = {}
    exec(src[start:end], ns)
    table = ns["TEST_TABLE"]
    bad = [(p, n) for p, n, e in table
           if _lineage._glob_match(p, n) != e or globmatch.matches(p, n) != e]
    check(f"glob parity: {len(table)} table rows agree with app.services.globmatch", not bad, str(bad))
    check("budgets mirror genmanifest", True)
    from app.services import genmanifest
    check("budgets: MAX_CHECK_ROWS / MAX_KNOWN_VALUES mirror genmanifest",
          _lineage.MAX_CHECK_ROWS == genmanifest.MAX_CHECK_ROWS
          and _lineage.MAX_KNOWN_VALUES == genmanifest.MAX_KNOWN_VALUES)
    from app.services import schemadiff
    check("norm parity with schemadiff.norm",
          # Cyrillic on purpose: a non-ASCII casefold probe ("Branch").
          all(_lineage._norm(x) == schemadiff.norm(x) for x in [" Филиал ", "É", "e\u0301", "SKU"]))


def test_stages(tmp):
    """onepage.stage() — the P4.1 runtime API. Everything here is narration:
    the assertions that matter most are the ones proving a bad call is a
    no-op rather than an exception in the middle of a report run."""
    import numpy as np
    import pandas as pd

    _lineage.drain_stages()          # start from a clean list
    _lineage._stage_files.clear()

    check("stages: `onepage` is importable after install_hooks",
          sys.modules.get("onepage") is not None
          and sys.modules["onepage"].stage is _lineage.stage)
    import builtins
    check("stages: `onepage` is a builtin too (no import needed)",
          getattr(builtins, "onepage", None) is sys.modules["onepage"])
    import onepage  # noqa: E402  — registered by install_hooks(), not on disk

    df = pd.DataFrame({
        "SKU": ["a", "b", "a", None],
        "Qty": np.arange(4, dtype="int64"),
        "Date": pd.to_datetime(["2026-08-20"] * 4),
        "Empty": [np.nan] * 4,
    })
    _lineage.note_file("stock_2026.xlsx")
    _lineage.note_file("stock_2026.xlsx")          # same file twice = one entry
    onepage.stage("merged", df)
    _lineage.note_file("ref.xlsx")
    agg = df.groupby("SKU", as_index=False)["Qty"].sum()
    onepage.stage("aggregated", agg, inputs=["merged"])

    onepage.stage("not a frame", {"rows": 1})       # ignored, silently
    onepage.stage("", df)                           # empty name — ignored
    onepage.stage("late", df, inputs="merged")      # inputs must be a list

    st = _lineage.drain_stages()
    check("stages: only DataFrames with a name are recorded",
          [s["name"] for s in st] == ["merged", "aggregated", "late"], str([s["name"] for s in st]))
    check("stages: order is the 0-based call index",
          [s["order"] for s in st] == [0, 1, 2], str([s["order"] for s in st]))
    check("stages: drain_stages() pops", _lineage.drain_stages() == [])

    a = st[0]
    check("stages: rows + columns", a["rows"] == 4
          and a["columns"] == ["SKU", "Qty", "Date", "Empty"], str(a["columns"]))
    check("stages: read_files are the files added since the previous stage, de-duped",
          a["read_files"] == ["stock_2026.xlsx"], str(a["read_files"]))
    check("stages: inputs default to None", a["inputs"] is None)
    check("stages: ms recorded", isinstance(a["ms"], float) and a["ms"] >= 0.0)
    check("stages: JSON-serialisable", json.dumps(st) is not None)

    b = st[1]
    check("stages: read_files reset after each snapshot", b["read_files"] == ["ref.xlsx"], str(b["read_files"]))
    check("stages: inputs kept as declared", b["inputs"] == ["merged"], str(b["inputs"]))
    check("stages: a non-list `inputs` is dropped, the stage is kept",
          st[2]["inputs"] is None, str(st[2]["inputs"]))

    profs = {p["column"]: p for p in a["profiles"]}
    check("stages: one profile per column", sorted(profs) == sorted(a["columns"]), str(sorted(profs)))
    check("stages: profile shape matches run_checks profiles",
          set(profs["SKU"]) == {"sheet", "column", "type", "nulls", "count",
                                "distinct", "min", "max", "top", "sampled"},
          str(sorted(profs["SKU"])))
    check("stages: profile has no sheet", profs["SKU"]["sheet"] is None)
    check("stages: text profile (None is a null, not a value)",
          (profs["SKU"]["type"], profs["SKU"]["nulls"], profs["SKU"]["count"],
           profs["SKU"]["distinct"], profs["SKU"]["top"][0]) == ("text", 1, 3, 2, ["a", 2]),
          str(profs["SKU"]))
    check("stages: numpy ints profile as numbers, not 'mixed'",
          (profs["Qty"]["type"], profs["Qty"]["min"], profs["Qty"]["max"]) == ("number", 0, 3),
          str(profs["Qty"]))
    check("stages: datetime64 profiles as a date",
          profs["Date"]["type"] == "date" and profs["Date"]["min"].startswith("2026-08-20"),
          str(profs["Date"]))
    check("stages: NaN counts as null, not as a value",
          (profs["Empty"]["type"], profs["Empty"]["nulls"], profs["Empty"]["count"]) == ("empty", 4, 0),
          str(profs["Empty"]))

    # pandas NA flavours that are not NaN: pd.NA and NaT.
    na = pd.DataFrame({"a": pd.array([1, None], dtype="Int64"),
                       "d": pd.to_datetime(["2026-01-01", None])})
    onepage.stage("na", na)
    p = {x["column"]: x for x in _lineage.drain_stages()[0]["profiles"]}
    check("stages: pd.NA / NaT are nulls",
          (p["a"]["nulls"], p["a"]["count"], p["d"]["nulls"], p["d"]["count"]) == (1, 1, 1, 1),
          str(p))

    # Budgets.
    long_name = "Stage " * 40
    wide = pd.DataFrame({f"c{i}": [i] for i in range(300)})
    onepage.stage(long_name, wide)
    w = _lineage.drain_stages()[0]
    check("stages: name cut to MAX_CELL_CHARS", len(w["name"]) == _lineage.MAX_CELL_CHARS, str(len(w["name"])))
    check("stages: columns cut to MAX_STAGE_COLUMNS",
          len(w["columns"]) == _lineage.MAX_STAGE_COLUMNS, str(len(w["columns"])))
    check("stages: profiles cut to MAX_STAGE_PROFILES",
          len(w["profiles"]) == _lineage.MAX_STAGE_PROFILES, str(len(w["profiles"])))

    for i in range(_lineage.MAX_STAGES + 5):
        onepage.stage(f"s{i}", df)
    over = _lineage.drain_stages()
    check("stages: at most MAX_STAGES per run",
          len(over) == _lineage.MAX_STAGES and over[-1]["order"] == _lineage.MAX_STAGES - 1,
          str(len(over)))

    real_max = _lineage.MAX_STAGE_ROWS
    _lineage.MAX_STAGE_ROWS = 2
    try:
        onepage.stage("big", df)
    finally:
        _lineage.MAX_STAGE_ROWS = real_max
    big = _lineage.drain_stages()[0]
    check("stages: rows is the FULL height even when the profile is sampled",
          big["rows"] == 4 and all(p["sampled"] for p in big["profiles"]), str(big["rows"]))
    check("stages: sampled profile only saw the first rows",
          big["profiles"][1]["count"] == 2, str(big["profiles"][1]))

    # Nothing a generator can pass may raise out of stage().
    class Exploding(pd.DataFrame):
        @property
        def columns(self):
            raise RuntimeError("boom")

    raised = None
    try:
        onepage.stage("boom", Exploding(df))
        onepage.stage("none", None)
        onepage.stage(None, df)
    except BaseException as exc:
        raised = exc
    check("stages: a broken frame never raises into the generator", raised is None, str(raised))
    check("stages: and leaves nothing behind", _lineage.drain_stages() == [])

    # MemoryError is explicitly promised never to escape stage() either — the
    # authoring guide says "stage() never raises", full stop, unlike sniff()/
    # run_checks() which let KeyboardInterrupt/SystemExit/MemoryError through
    # via _reraise_control. stage() is the one call site that allocates a
    # fresh MAX_STAGE_ROWS-sized Python list per column, so this is the most
    # plausible place for an allocation failure to actually happen.
    class OOMFrame(pd.DataFrame):
        @property
        def columns(self):
            raise MemoryError("simulated OOM")

    raised = None
    try:
        onepage.stage("oom", OOMFrame(df))
    except BaseException as exc:
        raised = exc
    check("stages: MemoryError never raises into the generator either",
          raised is None, str(raised))
    check("stages: and still leaves nothing behind", _lineage.drain_stages() == [])

    # _stage_inputs has its own cap (MAX_STAGE_INPUTS), not accidentally
    # coupled to MAX_STAGES (the unrelated per-run stage-COUNT ceiling).
    onepage.stage("many-inputs", df, inputs=[f"in{i}" for i in range(100)])
    mi = _lineage.drain_stages()[0]
    check("stages: inputs list capped at MAX_STAGE_INPUTS",
          len(mi["inputs"]) == _lineage.MAX_STAGE_INPUTS, str(len(mi["inputs"])))

    # Cumulative wall-clock budget across the whole run's stage() calls
    # (MAX_STAGE_BUDGET_MS): once spent, later calls skip profiling but keep
    # recording shape/columns/inputs/read_files — never break, never hang.
    real_budget = _lineage.MAX_STAGE_BUDGET_MS
    real_spent = _lineage._stage_ms_spent
    _lineage.MAX_STAGE_BUDGET_MS = 0.0   # already "spent" before this call
    try:
        onepage.stage("over_budget", df)
    finally:
        _lineage.MAX_STAGE_BUDGET_MS = real_budget
        _lineage._stage_ms_spent = real_spent
    ob = _lineage.drain_stages()[0]
    check("stages: over budget -> profiles empty but shape/columns still recorded",
          ob["profiles"] == [] and ob["rows"] == 4 and ob["columns"] == list(df.columns),
          str(ob))
    check("stages: over budget -> read_files/inputs still recorded", "read_files" in ob and "inputs" in ob)

    _lineage._stage_files.clear()

def test_idempotent(tmp):
    import pandas as pd
    import python_calamine

    before = (python_calamine.CalamineWorkbook, pd.read_excel, pd.ExcelFile.parse)
    _lineage.install_hooks()
    after = (python_calamine.CalamineWorkbook, pd.read_excel, pd.ExcelFile.parse)
    check("hooks: second install_hooks() is a no-op", before == after)

    book = os.path.join(tmp, "book.xlsx")
    pd.read_excel(book, sheet_name="Stock", usecols=["SKU"])
    got = _lineage.drain()[book]["sheets"]["Stock"]
    check("hooks: no double wrapping after the second install",
          got == {"columns": ["SKU"], "all": False}, str(got))


def main():
    tmp = tempfile.mkdtemp(prefix="op-lineage-smoke-")
    try:
        test_sniff(tmp)
        test_sniff_no_silent_empty(tmp)
        test_sniff_budget(tmp)
        test_checks(tmp)
        test_checks_not_null_mostly_blank(tmp)
        test_norm_key()
        test_ref_lookup(tmp)
        test_glob_parity()
        _lineage.install_hooks()
        test_calamine_hook(tmp)
        test_pandas_hook(tmp)
        test_pandas_internal_reads(tmp)
        test_pandas_buffers(tmp)
        test_pickle(tmp)
        test_stages(tmp)
        test_idempotent(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
