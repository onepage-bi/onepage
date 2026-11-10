#!/usr/bin/env python3
"""Regenerate tests/engine2/fixtures/*.xlsx.

Synthetic, tiny, and committed — the real SL exports are company data and never
enter the repo, but every shape that broke the Python engine is reproduced here
so the Node tests can run on any machine with no data mount:

  mixed.xlsx    duplicate headers, an `Unnamed:`-style blank header, a header
                that literally spells `__partition`, a numeric column with 1C's
                `' '` blanks, a column mixing real dates with
                'dd.mm.yyyy hh:mm:ss' text and Excel serials, a time-of-day
                column, an all-blank column.
  part_a.xlsx   same table, one partition …
  part_b.xlsx   … and another whose `qty` is text where A's is numeric — the
                type conflict `_rebuild_view` exists to survive.

    .venv/bin/python tests/engine2/make_fixtures.py
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).resolve().parent / "fixtures"


def mixed() -> Workbook:
    # Cyrillic on purpose: this is the shape of a real 1C export, and the
    # headers exercise Cyrillic case-folding ("Дата"/"дата"/"ДАТА" -> one key).
    wb = Workbook()
    ws = wb.active
    ws.title = "Лист1"
    ws.append(["Дата", "дата", "  ДАТА  ", "", "__partition",
               "Заказ, шт", "Тр. дата поставки", "Время", "Пусто"])
    rows = [
        # dates             dup      dup2   blank hdr  reserved  qty     mixed date/text                      time         empty
        [dt.date(2026, 7, 1), "a", "x", "z1", "p1", 10, dt.datetime(2026, 7, 1, 10, 30), dt.time(9, 15), None],
        [dt.date(2026, 7, 2), "b", "y", "z2", "p2", " ", "02.07.2026 08:00:00", dt.time(10, 0), None],
        [dt.date(2026, 7, 3), "c", "z", "z3", "p3", 30.5, 46205.5, dt.time(23, 59, 59), None],
        [dt.date(2026, 7, 4), "d", "w", "z4", "p4", None, None, None, None],
    ]
    for r in rows:
        ws.append(r)
    return wb


def partition(qty_as_text: bool) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(["Client", "qty", "Date"])
    for i in range(5):
        qty = f"{i * 10}" if qty_as_text else i * 10
        ws.append([f"client {i}", qty, dt.date(2026, 8, i + 1)])
    if qty_as_text:
        # 1C writes a single space where a number is missing — the cell that
        # made DuckDB guess INT32 and die.
        ws.append(["client x", " ", dt.date(2026, 8, 9)])
    return wb


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    mixed().save(HERE / "mixed.xlsx")
    partition(False).save(HERE / "part_a.xlsx")
    partition(True).save(HERE / "part_b.xlsx")
    for p in sorted(HERE.glob("*.xlsx")):
        print(f"   {p.name}  ({p.stat().st_size} B)")


if __name__ == "__main__":
    main()
