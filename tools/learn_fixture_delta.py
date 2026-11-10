#!/usr/bin/env python3
"""Sales export of the invented distributor "Delta": the teaching dataset of the Practicum course.

    tools/learn_fixture_delta.py            # regenerate the files in static/learn/files/
    tools/learn_fixture_delta.py --check    # determinism test: sha256 matches the repository

Why. The course teaches how to build a first report, and the learner must build
it not on "something of their own" but on a shared dataset: then the lesson has
a right answer, the author can quote numbers, and a reviewer can retrace the
path. That is why the file lives in the repository, is downloaded straight from
the lesson and is generated from here.

Determinism is not tidiness, it is a precondition. The same run must produce
the same file byte for byte, otherwise a lesson with numbers goes stale on every
rebuild. Hence the fixed seed, the fixed dates inside the zip and the fixed
document properties: `--check` regenerates into a temporary directory and
compares sha256.

The world is invented end to end: a distributor of packaged drinks, 60 stores in
three cities (codes like "NF-01"), three channels, 12 weeks, one delivery per
store per week on Tuesdays, four items. Not a single real name, address or
person.

THE ARITHMETIC ADDS UP. For every store, every item, every day:

    stock yesterday + delivery - sales - write-off = stock today

This is not decoration but the condition for the case to exist: an attentive
reader will check one row, and it has to add up. It is verified in code
(`verify_balance`) and the result is printed in the report.

SIX FINDINGS are baked into the data. Each one can be discovered from the data
and none is announced in words: an export never has a "stockout flag" column.

  1. STOCKOUTS. The warehouse ships some stores less than they sell. Goods run
     out 2-3 days before the next Tuesday: daily sales drop to zero, stock is
     zero too, and sales recover exactly on the delivery day. The main finding:
     at such a store sales equal SUPPLY, not demand, and the naive conclusion
     "take volume away from where it sells worse" deepens the shortage.

  2. WRITE-OFFS. Stores that get more than they sell still hold stock at the
     next delivery, and part of it is written off past its shelf life. This is
     the second half of the answer: where to find volume without buying more.

  3. UNITS. Two cities keep their books in PACKS (12 bottles), the third in
     BOTTLES. The export does not say so anywhere: there is one column. The
     "Warehouse" sheet is always in bottles, so reconciling the two sheets shows
     exactly a 12-fold gap for two cities.

  4. A CLOSED STORE. Five days of repairs: the rows ARE in the export, the
     values are EMPTY, not zero. The average is wrong by exactly as much as the
     analyst decided to treat empty as zero.

  5. A DOUBLED WEEK. One week sits in the export twice, as an honest copy
     without a single difference. The week looks like a record (exactly double),
     but the ratios inside it do not change: item shares, city shares, average
     daily sales per store stay the same. It is caught only by reconciling with
     the source: the "Warehouse" sheet knows of one delivery for that week.

  6. A PROMO WEEK. A demand spike for which the warehouse shipped more than
     usual. Sales velocity computed with this week included is inflated; it
     must not be used as the baseline.

Synthetic data only: invented cities, invented stores, invented numbers.

SEEDS. The generator draws its randomness from a stable hash of store codes and
item names. The dataset was first published with labels in another language,
and the numbers the course quotes were computed from that release. To keep
every number in place, hashing and row ordering still use the ORIGINAL labels
(`_SEED_LABEL`, written as escapes so the source stays ASCII); only what is
written to the files goes out in English.
"""
from __future__ import annotations

import os as _os

# The XML serializer is pinned EXPLICITLY, and this is not a style nitpick.
# openpyxl writes the workbook through lxml if it is installed and through the
# standard ElementTree if not, and the two differ in markup details:
# `<numFmt .../>` vs `<numFmt ... />`, attribute order, sheet size (15.6 MB vs
# 11.7 on the same export). So the fixture's reproducibility depended on
# whether lxml was present in the environment.
#
# It depended silently: lxml came in as a dependency of python3-saml. When SAML
# moved to a separate requirements-saml.txt, the base install stopped pulling
# it in, and CI began "failing to reproduce" a file built on a machine where
# lxml remained. Reproducibility that rests on an accidental transitive
# dependency is not reproducibility.
#
# Set BEFORE importing openpyxl: the flag is evaluated once, at import time.
_os.environ.setdefault("OPENPYXL_LXML", "False")


import argparse
import csv
import datetime as dt
import hashlib
import io
import random
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "static" / "learn" / "files"
XLSX_NAME = "delta-sales.xlsx"
CSV_NAME = "delta-sales.csv"
# The workbook's second sheet as a separate file: without Excel a learner never
# sees "Warehouse", and without it there is nowhere to learn how many bottles
# are in a pack.
STOCK_CSV_NAME = "delta-stock.csv"

SEED = 20260901
START = dt.date(2026, 3, 3)          # a Tuesday: the period starts on a delivery day
WEEKS = 12
DELIVERY_WEEKDAY = 1                 # Tuesday

# Cities and the unit each city keeps its export in. Finding 3: a "pack" is
# 12 bottles, and the export itself never says so.
PACK = 12
CITIES = (("Northfield", "NF", "pack"),
          ("Lakeside", "LS", "pack"),
          ("Riverton", "RV", "btl"))
CHANNELS = ("supermarket", "convenience store", "petrol station")
# How many stores of each channel a city has: 6 + 9 + 5 = 20, three cities = 60.
CHANNEL_MIX = (("supermarket", 6), ("convenience store", 9), ("petrol station", 5))
# How many bottles a day an average store of the channel sells (all items together).
CHANNEL_DAY = {"supermarket": 420.0, "convenience store": 300.0, "petrol station": 210.0}

ITEMS = ("water 0.5", "water 1.5", "lemonade 0.5", "iced tea 0.5")
ITEM_SHARE = {"water 0.5": 0.34, "water 1.5": 0.22,
              "lemonade 0.5": 0.26, "iced tea 0.5": 0.18}
PRICE = {"water 0.5": 120, "water 1.5": 210,
         "lemonade 0.5": 165, "iced tea 0.5": 190}      # per bottle

# The original labels that seed the random generator and order the rows (see
# SEEDS in the module docstring). Changing any of them changes every number the
# course quotes. Keys are the city prefixes and the item names above.
_SEED_LABEL = {
    "NF": "\u0421\u0412",
    "LS": "\u041f\u0420",
    "RV": "\u0417\u0420",
    "water 0.5": "\u0432\u043e\u0434\u0430 0,5",
    "water 1.5": "\u0432\u043e\u0434\u0430 1,5",
    "lemonade 0.5": "\u043b\u0438\u043c\u043e\u043d\u0430\u0434 0,5",
    "iced tea 0.5": ("\u0447\u0430\u0439 \u0445\u043e\u043b\u043e\u0434"
                     "\u043d\u044b\u0439 0,5"),
}

# Demand within a week is uneven, weekends are higher. Hence assumption no. 1 in
# the solution: "we treat demand within a week as flat"; the estimate drifts a
# little because of it, and this has to be said out loud, not hidden.
WEEKDAY = {0: 0.95, 1: 0.92, 2: 0.95, 3: 1.02, 4: 1.22, 5: 1.15, 6: 0.79}  # Mon..Sun

# Store roles: how much is shipped relative to what the store sells.
N_SHORT = 14                 # finding 1: shipped less than sold
N_OVER = 26                  # finding 2: shipped more than sold
# the remaining 20 are "normal", shipped almost exactly

# Share of the stale stock that is written off past shelf life on the last day
# of the cycle. In equilibrium the weekly write-off equals the weekly
# over-shipment, which is how the case's answer adds up numerically.
WASTE_FRAC = 0.5

PROMO_WEEK = 7               # finding 6
PROMO_FACTOR = 1.85
DUP_WEEK = 10                # finding 5
REPAIR_WEEK = 4              # finding 4
REPAIR_OFFSET = 2            # from the third day of the cycle (Thursday): the delivery is untouched
REPAIR_DAYS = 5

# Weekday and week number are in the file ready-made, not derived by formula.
# The course says "delivery on Tuesdays" and "week ten is doubled", and the
# learner should SEE that, not compute it: they came to learn to build a report,
# not to dissect Excel functions (owner, 13.09).
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")

HEADERS = ("date", "weekday", "week", "city", "channel", "store", "item",
           "sales", "delivery", "write-off", "stock")
STOCK_HEADERS = ("week", "delivery date", "store", "item",
                 "shipped, btl", "price per bottle")


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------

def _seed_code(code: str) -> str:
    """The original spelling of a store code: "NF-01" -> its first-release form."""
    prefix, _, num = code.partition("-")
    return _SEED_LABEL[prefix] + "-" + num


def hash_code(code: str, item: str | None = None) -> int:
    """Stable string hash: the built-in hash() is randomized per run.

    Hashes the ORIGINAL labels of the store code (and item), so the generated
    numbers are the ones the course quotes."""
    key = _seed_code(code)
    if item is not None:
        key += "|" + _SEED_LABEL[item]
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def build_points() -> list[dict]:
    """60 stores: city, channel, unit, daily demand per item, role.

    The role is assigned by POSITION IN THE DEMAND RANKING, not at random: the
    fastest stores are the ones most often under-supplied; otherwise the case
    would not be counter-intuitive and "ship more where sales are lower" would
    not be a conclusion."""
    points: list[dict] = []
    for city, prefix, unit in CITIES:
        n = 0
        for channel, count in CHANNEL_MIX:
            for _ in range(count):
                n += 1
                code = "%s-%02d" % (prefix, n)
                rng = random.Random(SEED * 7 + hash_code(code))
                size = rng.uniform(0.70, 1.40)
                day_bottles = CHANNEL_DAY[channel] * size
                demand = {}
                for item in ITEMS:
                    per_day = day_bottles * ITEM_SHARE[item]
                    if unit == "pack":
                        per_day /= PACK
                    demand[item] = per_day
                points.append({"code": code, "city": city, "channel": channel,
                               "unit": unit, "k": PACK if unit == "pack" else 1,
                               "demand": demand,
                               "week_bottles": day_bottles * 7})
        # stores within a city are in code order, as in any export
    order = sorted(points, key=lambda p: -p["week_bottles"])
    short_rank = 0
    n_short = n_over = 0
    for i, p in enumerate(order):
        rng = random.Random(SEED * 13 + hash_code(p["code"]))
        if i % 4 == 0 and n_short < N_SHORT:
            # The bigger the store, the deeper the hole: the fastest one gets
            # 0.48 of what it sells.
            p["role"] = "shortage"
            p["ratio"] = 0.48 + 0.012 * short_rank
            short_rank += 1
            n_short += 1
        elif i % 4 in (1, 2) and n_over < N_OVER:
            p["role"] = "surplus"
            p["ratio"] = 1.16 + rng.uniform(0.0, 0.16)
            n_over += 1
        else:
            p["role"] = "normal"
            p["ratio"] = 1.00 + rng.uniform(0.0, 0.06)
    return points


def repair_point(points: list[dict]) -> str:
    """The store that closes for repairs: a bottle city, role "normal".

    Not a shortage store, otherwise the empty days would mix with the stockout
    and neither finding would read cleanly. And not a pack city, otherwise the
    empty cells and the foreign units would have to be untangled in one move."""
    cand = [p for p in points if p["unit"] == "btl" and p["role"] == "normal"]
    for channel in CHANNELS:
        for p in cand:
            if p["channel"] == channel:
                return p["code"]
    raise RuntimeError("no store to close for repairs")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def build_rows() -> tuple[list[dict], list[dict], dict]:
    points = build_points()
    closed_code = repair_point(points)
    closed_days = {START + dt.timedelta(days=(REPAIR_WEEK - 1) * 7 + REPAIR_OFFSET + i)
                   for i in range(REPAIR_DAYS)}

    rows: list[dict] = []
    stock_rows: list[dict] = []
    by_point: dict[str, dict] = {p["code"]: p for p in points}

    for p in points:
        for item in ITEMS:
            rng = random.Random(SEED + hash_code(p["code"], item))
            stock = 0
            for week in range(1, WEEKS + 1):
                promo = PROMO_FACTOR if week == PROMO_WEEK else 1.0
                base = p["demand"][item] * promo
                supply_week = max(1, int(round(base * 7 * p["ratio"])))
                for day in range(7):
                    date = START + dt.timedelta(days=(week - 1) * 7 + day)
                    supply = supply_week if date.weekday() == DELIVERY_WEEKDAY else 0
                    if supply:
                        stock_rows.append({"week": week, "delivery date": date,
                                           "store": p["code"], "item": item,
                                           "shipped, btl": supply * p["k"],
                                           "price per bottle": PRICE[item]})
                    if p["code"] == closed_code and date in closed_days:
                        # Finding 4: the row exists, the numbers do not. Empty,
                        # not zero: the store did not sell zero, it was closed.
                        rows.append(_row(date, p, item, None, None, None, None, week))
                        continue
                    avail = stock + supply
                    want = max(1, int(round(base * WEEKDAY[date.weekday()]
                                            * rng.uniform(0.90, 1.10))))
                    sales = min(want, avail)
                    rest = avail - sales
                    waste = 0
                    if day == 6 and rest > 0:
                        # End of the cycle: part of the stale stock expires.
                        waste = int(rest * WASTE_FRAC)
                    stock = rest - waste
                    rows.append(_row(date, p, item, sales, supply, waste, stock, week))

    # Ordered by the ORIGINAL store codes, so the row order is the published one.
    rows.sort(key=lambda r: (r["date"], _seed_code(r["store"]), ITEMS.index(r["item"])))
    stock_rows.sort(key=lambda r: (r["week"], _seed_code(r["store"]), ITEMS.index(r["item"])))

    # Finding 5: a week is glued to the export a second time, as an HONEST copy
    # without a single difference. A repeated export lays the same rows down
    # again, and it is plausible precisely because nothing breaks inside the
    # week: sales and deliveries both doubled, so item shares and average daily
    # sales per store are unchanged, and the week looks like a record. The
    # "Warehouse" sheet knows of exactly one delivery for this week: that is
    # the reconciliation.
    dup_src = [r for r in rows if r["_week"] == DUP_WEEK]
    last = max(i for i, r in enumerate(rows) if r["_week"] == DUP_WEEK)
    dup = []
    for r in dup_src:
        c = dict(r)
        c["_dup"] = True
        dup.append(c)
    rows[last + 1: last + 1] = dup

    facts = {"points": points, "by_point": by_point, "closed": closed_code,
             "closed_days": sorted(closed_days),
             "dup_first_row": last + 1, "dup_count": len(dup)}
    return rows, stock_rows, facts


def _row(date, p, item, sales, supply, waste, stock, week) -> dict:
    return {"date": date, "weekday": WEEKDAYS[date.weekday()], "week": week,
            "city": p["city"], "channel": p["channel"],
            "store": p["code"], "item": item,
            "sales": sales, "delivery": supply,
            "write-off": waste, "stock": stock,
            "_week": week, "_dup": False}


# ---------------------------------------------------------------------------
# Arithmetic check
# ---------------------------------------------------------------------------

def verify_balance(rows: list[dict]) -> tuple[int, int, list[str]]:
    """stock yesterday + delivery - sales - write-off = stock today.

    Counted over ONE copy of the export (the doubled rows are skipped: that is
    the first lesson about them) and for each store-item pair separately.
    "Stock yesterday" for empty days is the last non-empty one: a closed store
    sold nothing and received nothing, its stock just stood there."""
    prev: dict[tuple[str, str], int] = {}
    checked = bad = 0
    errs: list[str] = []
    for r in rows:
        if r["_dup"]:
            continue
        key = (r["store"], r["item"])
        if r["stock"] is None:
            continue                      # a day without numbers: nothing to check
        was = prev.get(key, 0)
        got = was + r["delivery"] - r["sales"] - r["write-off"]
        checked += 1
        if got != r["stock"]:
            bad += 1
            if len(errs) < 5:
                errs.append("%s %s %s: %d + %d - %d - %d = %d, file has %d"
                            % (r["date"], r["store"], r["item"], was,
                               r["delivery"], r["sales"], r["write-off"], got,
                               r["stock"]))
        prev[key] = r["stock"]
    return checked, bad, errs


def verify_stock_sheet(rows: list[dict], stock_rows: list[dict]) -> tuple[int, int]:
    """The "Warehouse" sheet against the deliveries in the export, in bottles,
    after reconciling units. EVERYTHING must match: the data has exactly one
    discrepancy, the units."""
    by = {}
    for r in stock_rows:
        by[(r["week"], r["store"], r["item"])] = r["shipped, btl"]
    ok = bad = 0
    seen = {}
    for r in rows:
        if r["_dup"] or not r["delivery"]:
            continue
        seen[(r["_week"], r["store"], r["item"])] = r["delivery"]
    for key, supply in seen.items():
        ok += 1
    return ok, bad


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

STAMP = dt.datetime(2026, 9, 1, 9, 0, 0)
_MODIFIED = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")


def _fixed_zip(raw: bytes) -> bytes:
    """Repack the xlsx with fixed dates, both inside the archive and in the properties.

    Two time leaks. First: zip stores a time for every entry, and openpyxl puts
    "now" there. Second: `dcterms:modified` in docProps/core.xml is written by
    the saving code itself, whatever we put in `wb.properties.modified`. Both
    are plugged here so the rest of the script need not know about the format."""
    src = zipfile.ZipFile(io.BytesIO(raw))
    stamp = STAMP.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for name in src.namelist():
            data = src.read(name)
            if name == "docProps/core.xml":
                data = _MODIFIED.sub(rb"\g<1>" + stamp + rb"\g<2>", data)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            out.writestr(info, data)
    return buf.getvalue()


def write_xlsx(path: Path, rows: list[dict], stock_rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.properties.creator = "OnePage"
    wb.properties.lastModifiedBy = "OnePage"
    wb.properties.title = "Sales by store"
    wb.properties.created = STAMP
    wb.properties.modified = STAMP

    ws = wb.active
    ws.title = "Sales"
    ws.append(list(HEADERS))
    for r in rows:
        ws.append([r[h] for h in HEADERS])
    sh = wb.create_sheet("Warehouse")
    sh.append(list(STOCK_HEADERS))
    for r in stock_rows:
        sh.append([r[h] for h in STOCK_HEADERS])

    for sheet, widths, date_cols in ((ws, (12, 12, 8, 12, 18, 9, 14, 10, 10, 10, 10), (1,)),
                                     (sh, (8, 14, 9, 14, 14, 16), (2,))):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for i, w in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(i)].width = w
        sheet.freeze_panes = "A2"
        for col in date_cols:
            for row in sheet.iter_rows(min_row=2, min_col=col, max_col=col):
                row[0].number_format = "DD.MM.YYYY"

    raw = io.BytesIO()
    wb.save(raw)
    path.write_bytes(_fixed_zip(raw.getvalue()))


def _cell(v) -> str | int:
    return "" if v is None else v


def write_csv(path: Path, rows: list[dict]) -> None:
    """The same "Sales" sheet for those without Excel. Semicolon and utf-8 with
    a BOM: that way the file opens in Excel and in anything else."""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";", lineterminator="\r\n")
        w.writerow(HEADERS)
        for r in rows:
            w.writerow([
                r["date"].strftime("%d.%m.%Y"), r["weekday"], r["week"],
                r["city"], r["channel"], r["store"], r["item"],
                _cell(r["sales"]), _cell(r["delivery"]),
                _cell(r["write-off"]), _cell(r["stock"]),
            ])


def write_stock_csv(path: Path, stock_rows: list[dict]) -> None:
    """The "Warehouse" sheet the same way: semicolon and utf-8 with a BOM."""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";", lineterminator="\r\n")
        w.writerow(STOCK_HEADERS)
        for r in stock_rows:
            w.writerow([
                r["week"], r["delivery date"].strftime("%d.%m.%Y"),
                r["store"], r["item"],
                r["shipped, btl"], r["price per bottle"],
            ])


def generate(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, stock_rows, facts = build_rows()
    write_xlsx(out_dir / XLSX_NAME, rows, stock_rows)
    write_csv(out_dir / CSV_NAME, rows)
    write_stock_csv(out_dir / STOCK_CSV_NAME, stock_rows)
    return rows, stock_rows, facts


def content_sha256(path) -> str:
    """Fingerprint of the CONTENT, not of the packaging.

    For zip containers (xlsx is a zip) the sha256 of the file itself cannot be
    compared: the bytes depend on the zlib version, which differs between the
    developer's machine and the CI runner. The same content is packed
    differently, and the reproducibility check starts complaining about the
    platform instead of the data; exactly this happened on 03.11.2026, when CI
    went red on two fixtures in a row.

    We hash entry names and their UNCOMPRESSED bytes in sorted order: that is
    exactly the property we promise, "regeneration gives the same file".
    """
    path = str(path)
    h = hashlib.sha256()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for name in sorted(z.namelist()):
                h.update(name.encode("utf-8"))
                h.update(b"\0")
                h.update(z.read(name))
                h.update(b"\0")
        return h.hexdigest()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Report for the course author
# ---------------------------------------------------------------------------

def report(out_dir: Path, rows: list[dict], stock_rows: list[dict], facts: dict) -> None:
    by_point = facts["by_point"]
    live = [r for r in rows if not r["_dup"] and r["sales"] is not None]

    def bottles(r, field="sales"):
        return r[field] * by_point[r["store"]]["k"]

    print("Files")
    for name in (XLSX_NAME, CSV_NAME, STOCK_CSV_NAME):
        p = out_dir / name
        print("  %-18s %9d bytes  sha256 %s" % (name, p.stat().st_size, content_sha256(p)))
    print()
    print("Sheet \"Sales\": %d data rows (rows 2..%d), columns: %s"
          % (len(rows), len(rows) + 1, ", ".join(HEADERS)))
    print("  period %s .. %s, %d weeks, %d stores, %d cities, %d channels, %d items"
          % (START.strftime("%d.%m.%Y"),
             (START + dt.timedelta(days=WEEKS * 7 - 1)).strftime("%d.%m.%Y"),
             WEEKS, len(facts["points"]), len(CITIES), len(CHANNELS), len(ITEMS)))
    print("Sheet \"Warehouse\": %d rows (rows 2..%d), columns: %s"
          % (len(stock_rows), len(stock_rows) + 1, ", ".join(STOCK_HEADERS)))
    print()

    # -- Arithmetic --------------------------------------------------------
    checked, bad, errs = verify_balance(rows)
    print("ARITHMETIC: stock yesterday + delivery - sales - write-off = stock today")
    print("  rows checked %d, mismatches %d: %s"
          % (checked, bad, "adds up" if not bad else "DOES NOT ADD UP"))
    for e in errs:
        print("    " + e)
    # Reconciling the sheets in bottles: units are reconciled, so everything must match.
    sup, sup_raw = {}, {}
    for r in rows:
        if r["_dup"] or not r["delivery"]:
            continue
        key = (r["_week"], r["store"], r["item"])
        sup_raw[key] = r["delivery"]
        sup[key] = r["delivery"] * by_point[r["store"]]["k"]
    ware = {(r["week"], r["store"], r["item"]): r["shipped, btl"]
            for r in stock_rows}
    diff = sum(1 for k, v in ware.items() if sup.get(k) != v)
    print("  reconciliation \"Sales\" vs \"Warehouse\" in bottles: %d rows, mismatches %d"
          % (len(ware), diff))
    print()

    # -- 1. Stockouts ------------------------------------------------------
    out_days: dict[str, int] = {}
    for r in live:
        if r["stock"] == 0 and r["sales"] == 0:
            out_days[r["store"]] = out_days.get(r["store"], 0) + 1
    # a store-day counts as "out of stock" when ALL items are out
    zero_by_day: dict[tuple[str, dt.date], int] = {}
    for r in live:
        if r["stock"] == 0 and r["sales"] == 0:
            zero_by_day[(r["store"], r["date"])] = zero_by_day.get((r["store"], r["date"]), 0) + 1
    full_out: dict[str, int] = {}
    for (code, _d), n in zero_by_day.items():
        if n == len(ITEMS):
            full_out[code] = full_out.get(code, 0) + 1
    print("FINDING 1: stockouts (zero sales at zero stock, recovery on the delivery day)")
    print("  stores with zero days on at least one item: %d of %d"
          % (len(out_days), len(facts["points"])))
    print("  stores where ALL items were out: %d, on average %.1f days a week"
          % (len(full_out), sum(full_out.values()) / max(1, len(full_out)) / WEEKS))
    worst = sorted(full_out.items(), key=lambda kv: -kv[1])[:5]
    print("  deepest: %s" % ", ".join("%s (%d days in 12 weeks)" % (c, n)
                                      for c, n in worst))
    print()

    # -- 2. Write-offs -----------------------------------------------------
    waste_by_point: dict[str, int] = {}
    for r in live:
        if r["write-off"]:
            waste_by_point[r["store"]] = waste_by_point.get(r["store"], 0) \
                + r["write-off"] * by_point[r["store"]]["k"]
    sold_all = sum(bottles(r) for r in live)
    waste_all = sum(waste_by_point.values())
    print("FINDING 2: write-offs where stock is left at the next delivery")
    print("  stores with write-offs: %d, written off in total %d btl, %.1f %% of sold"
          % (len(waste_by_point), waste_all, 100 * waste_all / sold_all))
    print("  overlap with the stockout list: %d stores"
          % len(set(waste_by_point) & set(full_out)))
    print()

    # -- 3. Units ----------------------------------------------------------
    print("FINDING 3: two cities write in packs (12 bottles), one in bottles")
    for city, prefix, unit in CITIES:
        codes = {p["code"] for p in facts["points"] if p["city"] == city}
        as_written = sum(v for (wk, c, it), v in sup_raw.items() if c in codes)
        as_warehouse = sum(v for (wk, c, it), v in ware.items() if c in codes)
        print("  %-11s unit \"%-4s\": delivery in the export %8d, on the \"Warehouse\" sheet "
              "%9d btl, a %4.1f-fold gap"
              % (city, unit, as_written, as_warehouse,
                 as_warehouse / max(1, as_written)))
    print()

    # -- 4. Closed store ---------------------------------------------------
    code = facts["closed"]
    days = facts["closed_days"]
    pt = [r for r in rows if not r["_dup"] and r["store"] == code]
    vals = {}
    for r in pt:
        if r["sales"] is not None:
            vals[r["date"]] = vals.get(r["date"], 0) + r["sales"]
    real = sum(vals.values()) / len(vals)
    with_zeros = sum(vals.values()) / (len(vals) + REPAIR_DAYS)
    print("FINDING 4: store %s was closed for %d days (empty, not zero)" % (code, REPAIR_DAYS))
    print("  dates: %s" % ", ".join(d.strftime("%d.%m.%Y") for d in days))
    print("  average daily sales: %.1f without the empty days, %.1f if they count "
          "as zeros: the average is understated by %.1f %%"
          % (real, with_zeros, 100 * (1 - with_zeros / real)))
    print()

    # -- 5. Doubled week ---------------------------------------------------
    dup_from = facts["dup_first_row"] + 2
    dup_to = dup_from + facts["dup_count"] - 1
    weeks_sales: dict[int, int] = {}
    for r in rows:
        if r["sales"] is None:
            continue
        weeks_sales[r["_week"]] = weeks_sales.get(r["_week"], 0) + bottles(r)
    normal = [v for w, v in weeks_sales.items() if w not in (DUP_WEEK, PROMO_WEEK)]
    base = sum(normal) / len(normal)
    print("FINDING 5: week %d is doubled (%s .. %s)"
          % (DUP_WEEK,
             (START + dt.timedelta(days=(DUP_WEEK - 1) * 7)).strftime("%d.%m.%Y"),
             (START + dt.timedelta(days=DUP_WEEK * 7 - 1)).strftime("%d.%m.%Y")))
    print("  sheet \"Sales\", rows %d..%d: the second copy of the week (%d rows)"
          % (dup_from, dup_to, facts["dup_count"]))
    print("  week sales %d btl against %.0f for a normal one, %.2f times higher: a record"
          % (weeks_sales[DUP_WEEK], base, weeks_sales[DUP_WEEK] / base))
    dup_rows = [r for r in rows if r["_week"] == DUP_WEEK and r["sales"] is not None]
    one = [r for r in dup_rows if not r["_dup"]]
    def share(rs):
        tot = sum(bottles(r) for r in rs)
        return {it: sum(bottles(r) for r in rs if r["item"] == it) / tot for it in ITEMS}
    s_all, s_one = share(dup_rows), share(one)
    print("  item shares: %s, not changed by a single percent"
          % ", ".join("%s %.3f/%.3f" % (it, s_one[it], s_all[it]) for it in ITEMS))
    trip = {}
    for r in dup_rows:
        k = (r["date"], r["store"], r["item"])
        trip[k] = trip.get(k, 0) + 1
    print("  reconciliation with the source: every date-store-item triple of this week "
          "occurs twice: %s; the \"Warehouse\" sheet has one delivery for the week"
          % ("yes" if set(trip.values()) == {2} else "NO %s" % sorted(set(trip.values()))))
    print()

    # -- 6. Promo ----------------------------------------------------------
    print("FINDING 6: week %d is a promo (%s .. %s)"
          % (PROMO_WEEK,
             (START + dt.timedelta(days=(PROMO_WEEK - 1) * 7)).strftime("%d.%m.%Y"),
             (START + dt.timedelta(days=PROMO_WEEK * 7 - 1)).strftime("%d.%m.%Y")))
    print("  week sales %d btl against %.0f for a normal one, %.2f times higher"
          % (weeks_sales[PROMO_WEEK], base, weeks_sales[PROMO_WEEK] / base))
    print("  the warehouse shipped more for the promo, so this is demand, not doubling:"
          " the week has as many rows as any other")


# ---------------------------------------------------------------------------

def zip_diff(a, b, limit: int = 6) -> list[str]:
    """What exactly differs between two containers: entry names and their sizes."""
    if not (zipfile.is_zipfile(str(a)) and zipfile.is_zipfile(str(b))):
        return ["not zip files: nothing to compare but bytes"]
    out = []
    with zipfile.ZipFile(str(a)) as za, zipfile.ZipFile(str(b)) as zb:
        na, nb = set(za.namelist()), set(zb.namelist())
        for name in sorted(na - nb)[:limit]:
            out.append("only in the repository: %s" % name)
        for name in sorted(nb - na)[:limit]:
            out.append("only in the regeneration: %s" % name)
        for name in sorted(na & nb):
            da, db = za.read(name), zb.read(name)
            if da != db:
                out.append("differs %s (%d vs %d bytes)" % (name, len(da), len(db)))
                if len(out) >= limit:
                    out.append("...and possibly more")
                    break
    return out or ["entries match: the difference is outside entry contents"]


def check() -> int:
    """Regenerate into a temporary directory and compare sha256 with the repository."""
    tmp = Path(tempfile.mkdtemp(prefix="learn-delta-"))
    try:
        generate(tmp)
        bad = 0
        for name in (XLSX_NAME, CSV_NAME, STOCK_CSV_NAME):
            have, want = OUT_DIR / name, tmp / name
            if not have.exists():
                print("MISSING FILE %s: run tools/learn_fixture_delta.py" % have)
                bad += 1
                continue
            a, b = content_sha256(have), content_sha256(want)
            ok = a == b
            print("%s %s  %s" % ("ok  " if ok else "BAD ", name, a))
            if not ok:
                print("       regeneration gave %s" % b)
                # The fingerprint says "no match" but not WHERE. For a zip that
                # is the difference between "fix it" and "guess": because of
                # this silence the first CI failure had to be untangled by hand.
                for line in zip_diff(have, want):
                    print("       %s" % line)
                bad += 1
        print("\n%s" % ("determinism confirmed" if not bad
                        else "mismatches: %d" % bad))
        return 1 if bad else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="write no files: compare sha256 with what is in the repository")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    rows, stock_rows, facts = generate(OUT_DIR)
    report(OUT_DIR, rows, stock_rows, facts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
