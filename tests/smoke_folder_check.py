"""Smoke test for static/py/_folder_check.py under native CPython.

Same idiom as tests/smoke_explore_py.py: the module runs inside the Pyodide
worker for real, but its logic is plain Python + DuckDB SQL and is fully
observable here. Builds small workbooks with openpyxl, indexes them through
`_explore` exactly the way `gen-worker.js` does, then runs `_folder_check.run()`
and asserts on the findings.

    .venv/bin/python tests/smoke_folder_check.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.getcwd(), "static", "py"))
import _explore  # noqa: E402
import _folder_check  # noqa: E402
from openpyxl import Workbook  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def book(path, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "TDSheet"
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    wb.save(path)


def by_kind(findings, kind):
    return [f for f in findings if f["kind"] == kind]


# ── duplicate_key ────────────────────────────────────────────────────────
def test_duplicate_key(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "orders", "display_name": "Orders",
                                      "partitions": [{"key": "07"}, {"key": "08"}]}]})
    a = os.path.join(tmp, "Orders_07.xlsx")
    b = os.path.join(tmp, "Orders_08.xlsx")
    # "order" is unique WITHIN each file (and sorts before "total", so it is
    # the first key candidate scanned), but "A-1" reappears in the second
    # file — an overlapping-period folder, the generic duplicate signal.
    book(a, ["Order", "Total"], [("A-1", 100), ("A-2", 200)])
    book(b, ["Order", "Total"], [("A-1", 100), ("A-3", 300)])
    _explore.index_file(a, "TDSheet", 0, "orders", "07")
    _explore.index_file(b, "TDSheet", 0, "orders", "08")
    findings = _folder_check.run()
    dups = by_kind(findings, "duplicate_key")
    check("duplicate_key finding raised", len(dups) == 1, findings)
    if dups:
        check("duplicate_key names the right column", dups[0]["column"] == "order", dups[0])
        check("duplicate_key counts one repeated key", dups[0]["count"] == 1, dups[0])
        check("duplicate_key sample carries the repeated value", dups[0]["sample"] == ["A-1"], dups[0])
        check("duplicate_key severity is warn", dups[0]["severity"] == "warn", dups[0])


def test_no_duplicate_when_keys_distinct(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "orders2", "partitions": [{"key": "07"}, {"key": "08"}]}]})
    a = os.path.join(tmp, "O2_07.xlsx")
    b = os.path.join(tmp, "O2_08.xlsx")
    book(a, ["Order", "Total"], [("A-1", 100), ("A-2", 200)])
    book(b, ["Order", "Total"], [("B-1", 100), ("B-2", 300)])
    _explore.index_file(a, "TDSheet", 0, "orders2", "07")
    _explore.index_file(b, "TDSheet", 0, "orders2", "08")
    findings = _folder_check.run()
    check("no duplicate_key finding when keys never repeat", not by_kind(findings, "duplicate_key"), findings)


# ── blank_rate ───────────────────────────────────────────────────────────
def test_blank_rate(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "blanks", "partitions": [{"key": "07"}, {"key": "08"}]}]})
    a = os.path.join(tmp, "Bl_07.xlsx")
    b = os.path.join(tmp, "Bl_08.xlsx")
    # Column "manager" is filled in July, mostly blank in August.
    book(a, ["Number", "Manager"], [(i, "Smith") for i in range(10)])
    book(b, ["Number", "Manager"], [(i, "" if i < 5 else "Jones") for i in range(10)])
    _explore.index_file(a, "TDSheet", 0, "blanks", "07")
    _explore.index_file(b, "TDSheet", 0, "blanks", "08")
    findings = _folder_check.run()
    br = by_kind(findings, "blank_rate")
    check("blank_rate finding raised for the column that broke in one partition",
         any(f["column"] == "manager" for f in br), findings)
    manager = next((f for f in br if f["column"] == "manager"), None)
    if manager:
        check("blank_rate sample mentions the bad partition", any("08" in s for s in manager["sample"]), manager)
    check("blank_rate not raised for the always-filled column", not any(f["column"] == "number" for f in br), br)


# ── type_conflict ────────────────────────────────────────────────────────
def test_type_conflict(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "mixed", "partitions": [{"key": "07"}, {"key": "08"}]}]})
    a = os.path.join(tmp, "Mix_07.xlsx")
    b = os.path.join(tmp, "Mix_08.xlsx")
    # Partition 07: "amount" is purely numeric -> a numeric DuckDB type.
    # Partition 08: "amount" is purely TEXT ("unknown" everywhere, no blank/mixed
    # cells for `_coerce_mixed_cols` to smooth into a number) -> VARCHAR.
    # Two internally-consistent partitions that disagree with EACH OTHER —
    # the real-world case, distinct from a within-one-file mixed column.
    book(a, ["Number", "Amount"], [("M-1", 100), ("M-2", 200)])
    book(b, ["Number", "Amount"], [("M-3", "unknown"), ("M-4", "unknown")])
    _explore.index_file(a, "TDSheet", 0, "mixed", "07")
    _explore.index_file(b, "TDSheet", 0, "mixed", "08")
    findings = _folder_check.run()
    tc = by_kind(findings, "type_conflict")
    check("type_conflict finding raised for amount", any(f["column"] == "amount" for f in tc), findings)


def test_no_type_conflict_when_consistent(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "clean", "partitions": [{"key": "07"}, {"key": "08"}]}]})
    a = os.path.join(tmp, "Cl_07.xlsx")
    b = os.path.join(tmp, "Cl_08.xlsx")
    book(a, ["Number", "Amount"], [("M-1", 100), ("M-2", 200)])
    book(b, ["Number", "Amount"], [("M-3", 150), ("M-4", 250)])
    _explore.index_file(a, "TDSheet", 0, "clean", "07")
    _explore.index_file(b, "TDSheet", 0, "clean", "08")
    findings = _folder_check.run()
    check("no type_conflict when every partition agrees", not by_kind(findings, "type_conflict"), findings)


# ── small_partition ──────────────────────────────────────────────────────
def test_small_partition(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "sizes", "partitions": [{"key": "07"}, {"key": "08"}, {"key": "09"}]}]})
    a = os.path.join(tmp, "Sz_07.xlsx")
    b = os.path.join(tmp, "Sz_08.xlsx")
    c = os.path.join(tmp, "Sz_09.xlsx")
    book(a, ["N"], [(i,) for i in range(100)])
    book(b, ["N"], [(i,) for i in range(100)])
    book(c, ["N"], [(i,) for i in range(10)])   # < 30% of the median (100)
    _explore.index_file(a, "TDSheet", 0, "sizes", "07")
    _explore.index_file(b, "TDSheet", 0, "sizes", "08")
    _explore.index_file(c, "TDSheet", 0, "sizes", "09")
    findings = _folder_check.run()
    sp = by_kind(findings, "small_partition")
    check("small_partition finding raised", len(sp) == 1, findings)
    if sp:
        check("small_partition counts the one truncated file", sp[0]["count"] == 1, sp[0])
        check("small_partition severity is info", sp[0]["severity"] == "info", sp[0])
        check("small_partition sample names partition 09", any("09" in s for s in sp[0]["sample"]), sp[0])


# ── supplied checks (genmanifest.CHECK_TYPES shape, resolved to table/column) ─
def test_supplied_checks(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "chk", "partitions": [{"key": "07"}]}]})
    a = os.path.join(tmp, "Chk_07.xlsx")
    book(a, ["Code", "Status", "Qty"], [
        ("K-1", "ok", 5), ("K-2", "bad", -1), ("K-2", "ok", 3), ("K-3", None, 999), ("ZZZZ", "ok", 7),
    ])
    _explore.index_file(a, "TDSheet", 0, "chk", "07")

    checks = [
        {"type": "not_null", "table": "chk", "column": "status", "label": "Status filled"},
        {"type": "unique", "table": "chk", "column": "code", "label": "Code unique"},
        {"type": "range", "table": "chk", "column": "qty", "min": 0, "max": 100, "label": "Qty in range"},
        {"type": "enum", "table": "chk", "column": "status", "values": ["ok", "bad"], "label": "Status allowed"},
        {"type": "regex", "table": "chk", "column": "code", "pattern": "^K-\\d$", "label": "Code format"},
        # Should be silently skipped: unknown table / unknown column / bad type.
        {"type": "not_null", "table": "nope", "column": "x"},
        {"type": "not_null", "table": "chk", "column": "does not exist"},
        {"type": "totally_unknown", "table": "chk", "column": "code"},
    ]
    findings = _folder_check.run(checks)

    nn = by_kind(findings, "check_not_null")
    check("check_not_null caught the one blank status", len(nn) == 1 and nn[0]["count"] == 1, nn)

    uq = by_kind(findings, "check_unique")
    check("check_unique caught the repeated code", len(uq) == 1 and uq[0]["count"] == 1, uq)
    if uq:
        check("check_unique sample carries the repeated code", uq[0]["sample"] == ["K-2"], uq[0])

    rg = by_kind(findings, "check_range")
    check("check_range caught the negative quantity", len(rg) == 1 and rg[0]["count"] == 2, rg)
    # -1 (below min) and 999 (above max) are both out of [0, 100].

    en = by_kind(findings, "check_enum")
    check("check_enum flags nothing beyond ok/bad among non-null statuses", len(en) == 0, en)

    rx = by_kind(findings, "check_regex")
    check("check_regex flags codes that don't match ^K-\\d$", len(rx) >= 1, rx)


# ── sample/message shape ─────────────────────────────────────────────────
def test_finding_shape(tmp):
    _explore.reset()
    _explore.set_catalog({"tables": [{"name": "shape_t", "partitions": [{"key": "07"}, {"key": "08"}]}]})
    a = os.path.join(tmp, "Sh_07.xlsx")
    b = os.path.join(tmp, "Sh_08.xlsx")
    book(a, ["Order"], [("A-1",), ("A-2",)])
    book(b, ["Order"], [("A-1",), ("A-3",)])
    _explore.index_file(a, "TDSheet", 0, "shape_t", "07")
    _explore.index_file(b, "TDSheet", 0, "shape_t", "08")
    findings = _folder_check.run()
    check("every finding has the required keys",
         all({"kind", "severity", "table", "column", "count", "sample", "message"} <= set(f) for f in findings),
         findings)
    check("every sample is capped at 5 entries", all(len(f["sample"]) <= 5 for f in findings), findings)
    check("every sample value is capped at 64 chars",
         all(len(s) <= 64 for f in findings for s in f["sample"]), findings)
    check("every message is a non-empty Russian string",
         all(isinstance(f["message"], str) and f["message"] for f in findings), findings)


def main():
    tmp = tempfile.mkdtemp(prefix="op-folder-check-")
    try:
        test_duplicate_key(tmp)
        test_no_duplicate_when_keys_distinct(tmp)
        test_blank_rate(tmp)
        test_type_conflict(tmp)
        test_no_type_conflict_when_consistent(tmp)
        test_small_partition(tmp)
        test_supplied_checks(tmp)
        test_finding_shape(tmp)
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
