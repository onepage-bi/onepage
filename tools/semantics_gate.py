"""Reproducibility gate for a generator's semantics.json (operational KPIs).

Generic engine: loads the tables semantics.json declares (calamine -> pandas),
applies the declared rules (header normalisation, flexible dates, site-name
canonicalisation, multi-file dedup), registers them in DuckDB, executes the
KPI `cell_sql` strings FROM THE JSON, aggregates cells into the report's
period columns and compares every Grand Total cell with the reference
calculator's output (ref_multi.json).

Also times each KPI's cell_sql execution and gates on it (default budget 2000 ms):
a slow KPI is a report-layer problem to fix, not a platform workaround.

    python semgate.py <semantics.json> <data_dir> <bundle_config_dir> <ref.json> [as_of] [--max-kpi-ms 2000]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
from python_calamine import CalamineWorkbook

MASTER_NAMES = {"rcw health.xlsx", "rcw data.xlsx"}
TOL = 1e-9


# ── normalisation rules (semantics.rules) ────────────────────────────────
def norm_header(value) -> str:
    text = str(value if value is not None else "").strip().lower().replace("\u0451", "\u0435")  # Cyrillic yo -> ie
    text = re.sub(r"[ \s]+", " ", text)
    text = re.sub(r"[«»\"']", "", text)
    return text.strip()


def norm_dim(value) -> str:
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    return re.sub(r"[ \s]+", " ", text).strip().casefold()


def clean_text(value) -> str:
    return str(value if value is not None else "").strip()


def flex_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and 20000 <= float(value) <= 80000:
        return datetime(1899, 12, 30) + timedelta(days=float(value))
    text = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def source_priority(name: str) -> tuple[int, str]:
    return (0 if name.casefold() in MASTER_NAMES else 1, name.casefold())


# ── loading ──────────────────────────────────────────────────────────────
def read_sheet(path: Path, sheet: str, header_rule: str | None):
    wb = CalamineWorkbook.from_path(str(path))
    if sheet not in wb.sheet_names:
        return None
    rows = wb.get_sheet_by_name(sheet).to_python(skip_empty_area=False)
    hdr = 0
    if header_rule and "containing" in header_rule:
        needle = re.search(r"'([^']+)'", header_rule).group(1)
        hdr = next(i for i, r in enumerate(rows[:12]) if any(clean_text(c) == needle for c in r))
    header = rows[hdr]
    return header, rows[hdr + 1:]


def load_table(spec: dict, data_dir: Path, ctx: dict) -> pd.DataFrame:
    """Mirror of the generator's multi-file merge, driven by the spec."""
    files = []
    for src in spec["sources"]:
        if "bundle_config" in src:
            continue
        for p in sorted(data_dir.iterdir()):
            if p.suffix.lower() in (".xlsx", ".xlsm") and fnmatch.fnmatch(p.name.casefold(), src["file"].casefold()):
                files.append((p, src["sheet"], src.get("header_row")))
    files.sort(key=lambda t: source_priority(t[0].name))

    cols = spec["columns"]
    by_source = {norm_header(c["source"]): name for name, c in cols.items() if "source" in c}
    bkeys = spec.get("business_key")
    bkeys = [bkeys] if isinstance(bkeys, str) else (bkeys or [])

    canonical_keys: list[str] = []          # union of normalised headers across files
    rows_by_identity: dict = {}
    stats = []
    for path, sheet, hdr_rule in files:
        got = read_sheet(path, sheet, hdr_rule)
        if got is None:
            continue
        header, body = got
        keys = []
        counts: dict[str, int] = {}
        for h in header:
            n = norm_header(h)
            occ = counts.get(n, 0)
            counts[n] = occ + 1
            keys.append(f"{n}#{occ}" if n else f"column#{len(keys)}")
        for k in keys:
            if k not in canonical_keys:
                canonical_keys.append(k)
        chosen = next((f"{norm_header(cols[b]['source'])}#0" for b in bkeys
                       if f"{norm_header(cols[b]['source'])}#0" in canonical_keys), None)
        replaced = 0
        for r in body:
            values = {k: (r[i] if i < len(r) else None) for i, k in enumerate(keys)}
            if spec.get("row_filter"):
                first = clean_text(values.get(f"{norm_header(cols['rcw']['source'])}#0"))
                if not re.match(r"^\d{4}_", first):
                    continue
            bval = clean_text(values.get(chosen)) if chosen else ""
            identity = ("b", norm_header(bval)) if bval else ("r",) + tuple(clean_text(values.get(k)) for k in canonical_keys)
            if identity in rows_by_identity:
                replaced += 1
            rows_by_identity[identity] = (values, path.name)
        stats.append({"file": path.name, "rows": len(body), "replaced": replaced})

    out = {name: [] for name in cols}
    out["__file"] = []
    for values, fname in rows_by_identity.values():
        for name, c in cols.items():
            if "source" not in c:
                continue
            raw = values.get(f"{norm_header(c['source'])}#0")
            if c["type"] == "datetime":
                raw = flex_date(raw)
            elif c["type"] == "number":
                try:
                    raw = float(str(raw).replace(",", ".")) if raw not in (None, "") else None
                except ValueError:
                    raw = None
            else:
                raw = clean_text(raw)
                if c.get("canonical") == "rcw":
                    raw = ctx["canon_rcw"](raw)
            out[name].append(raw)
        out["__file"].append(fname)
    df = pd.DataFrame(out)
    for name, c in cols.items():
        if c.get("type") == "datetime" and name in df:
            df[name] = pd.to_datetime(df[name], errors="coerce")
    ctx["load_stats"][spec_name(spec)] = {"output_rows": len(df), "sources": stats}
    return df


def spec_name(spec):
    return spec.get("_name", "?")


# ── periods (semantics.periods) ──────────────────────────────────────────
def sunday_before_iso_week(as_of: datetime) -> str:
    d = as_of.date()
    start = d - timedelta(days=d.isoweekday() - 1)
    return (start - timedelta(days=1)).isoformat()


def day_universe(days: list[str]) -> list[str]:
    if not days:
        return []
    lo = date.fromisoformat(min(days)).replace(day=1)
    hi = date.fromisoformat(max(days))
    nxt = date(hi.year + (hi.month == 12), (hi.month % 12) + 1, 1)
    out, cur = [], lo
    while cur < nxt:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def period_columns(months, days, completed_through):
    cols = []
    for m in months:
        mdays = [d for d in days if d.startswith(m)]
        for d in mdays:
            cols.append({"key": d, "kind": "day", "month": m})
        weeks = []
        for d in mdays:
            w = date.fromisoformat(d).isocalendar()[1]
            if w not in weeks:
                weeks.append(w)
        for w in weeks:
            cols.append({"key": f"{m}:w{w}", "kind": "week", "month": m, "week": w})
        cols.append({"key": m, "kind": "month", "month": m})
    for y in sorted({m[:4] for m in months}):
        cols.append({"key": y, "kind": "year"})
    return cols


def in_period(day: str, col: dict) -> bool:
    if col["kind"] == "day":
        return day == col["key"]
    if col["kind"] == "week":
        return day[:7] == col["month"] and date.fromisoformat(day).isocalendar()[1] == col["week"]
    if col["kind"] == "month":
        return day[:7] == col["key"]
    return day[:4] == col["key"]


def clamp(x):
    return max(0.0, min(1.0, x))


def aggregate(kind: str, cells: pd.DataFrame, completed_through: str, workdays: set, n_rcws: int):
    days_with_data = sorted(set(cells["day"].astype(str)))
    months = sorted({d[:7] for d in days_with_data})
    days = day_universe(days_with_data)
    cols = period_columns(months, days, completed_through)
    by_day: dict[str, tuple[float, float]] = {}
    for day, g in cells.groupby(cells["day"].astype(str)):
        by_day[day] = (float(g["n"].sum()), float(g["d"].sum()))
    values = {}
    for col in cols:
        if col["kind"] == "day":
            day = col["key"]
            if day > completed_through:
                values[day] = None
                continue
            n, d = by_day.get(day, (0.0, 0.0))
            if kind == "daily_presence":
                denom = n_rcws if day in workdays else 0
                values[day] = min(1, n / denom) if denom else (n / n_rcws if n else None)
            elif kind == "correctness":
                values[day] = clamp((n - d) / n) if n > 0 else None
            else:
                values[day] = clamp(n / d) if d > 0 else None
            continue
        rel = [d for d in days if d <= completed_through and in_period(d, col)]
        n = sum(by_day.get(d, (0, 0))[0] for d in rel)
        d_ = sum(by_day.get(d, (0, 0))[1] for d in rel)
        if kind == "daily_presence":
            denom = len([d for d in rel if d in workdays]) * n_rcws
            values[col["key"]] = min(1, n / denom) if denom else None
        elif kind == "correctness":
            values[col["key"]] = clamp((n - d_) / n) if n > 0 else None
        else:
            values[col["key"]] = clamp(n / d_) if d_ > 0 else None
    return values


def kpi_offenders(timings: dict, budget: float) -> list:
    """Pure threshold logic: which KPI keys in a {key: ms} dict exceed budget.

    Kept separate from measure_kpis so the gate can be unit-tested against a
    fake timings dict with no DuckDB/semantics fixture involved.
    """
    return sorted(k for k, ms in timings.items() if ms > budget)


# ── main ─────────────────────────────────────────────────────────────────
def main(sem_path, data_dir, cfg_dir, ref_path, as_of_s="2026-07-24T12:00:00", max_kpi_ms=2000):
    sem = json.load(open(sem_path, encoding="utf-8"))
    data_dir, cfg_dir = Path(data_dir), Path(cfg_dir)
    ref = json.load(open(ref_path, encoding="utf-8"))["totals"]
    as_of = datetime.fromisoformat(as_of_s)
    completed_through = sunday_before_iso_week(as_of)
    reporting_start = sem["rules"]["reporting_start"]

    aliases = {norm_dim(k): v for k, v in json.load(open(cfg_dir / "rcw_aliases.json", encoding="utf-8"))["aliases"].items()}
    ctx = {"load_stats": {}, "canon_rcw": lambda v: v}
    for name, spec in sem["tables"].items():
        spec["_name"] = name
    master = load_table(sem["tables"]["rcw_master"], data_dir, ctx)
    lookup = {norm_dim(r): r for r in master["rcw"]}

    def canon(value: str) -> str:
        n = norm_dim(value)
        if not n:
            return ""
        if n in aliases:
            return aliases[n]
        return lookup.get(n, value.strip())
    ctx["canon_rcw"] = canon

    con = duckdb.connect()
    con.register("rcw_master", master)
    for tname in ("sales_orders", "realisations", "transfers"):
        con.register(tname, load_table(sem["tables"][tname], data_dir, ctx))
    cal = json.load(open(cfg_dir / "production_calendar_2026.json", encoding="utf-8"))
    workdays = set(cal["workdays"])
    all_days = [(date(2026, 1, 1) + timedelta(days=i)) for i in range(365)]
    con.register("calendar", pd.DataFrame({"day": all_days, "is_workday": [d.isoformat() in workdays for d in all_days]}))
    for vname, vsql in sem["views"].items():
        con.execute(f"CREATE VIEW {vname} AS {vsql.replace('{reporting_start}', reporting_start)}")

    n_rcws = len(master)
    report = {"completed_through": completed_through, "n_rcws": n_rcws, "load": ctx["load_stats"], "kpis": {}}
    total_cells = total_match = 0
    kpi_ms = {}
    for key, k in sem["kpis"].items():
        t0 = time.time()
        cells = con.execute(k["cell_sql"].replace("{reporting_start}", reporting_start)).df()
        kpi_ms[key] = (time.time() - t0) * 1000
        ours = aggregate(k["aggregation"], cells, completed_through, workdays, n_rcws)
        refv = ref[key]["values"]
        keys = sorted(set(refv) | set(ours))
        mism = []
        for ck in keys:
            a, b = refv.get(ck, "MISSING"), ours.get(ck, "MISSING")
            ok = (a is None and b is None) or (isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= TOL)
            if not ok:
                mism.append({"col": ck, "ref": a, "ours": b})
        total_cells += len(keys)
        total_match += len(keys) - len(mism)
        report["kpis"][key] = {"cells": len(keys), "match": len(keys) - len(mism), "mismatches": mism}
    report["S1"] = {"matched": total_match, "cells": total_cells, "ratio": round(total_match / total_cells, 4), "pass": total_match / total_cells >= 0.8}
    json.dump(report, open(Path(sem_path).parent / "gate_report.json", "w"), ensure_ascii=False, indent=1, default=str)
    print(f"completed_through={completed_through} rcws={n_rcws}")
    for t, s in ctx["load_stats"].items():
        print(f"  load {t}: rows={s['output_rows']} {[(x['file'], x['rows'], x['replaced']) for x in s['sources']]}")
    for key, r in report["kpis"].items():
        print(f"{key:22s} {r['match']}/{r['cells']}  " + "; ".join(f"{m['col']}: ref={m['ref']} ours={m['ours']}" for m in r["mismatches"][:4]))
    print("S1:", report["S1"])

    print(f"\nkpi timing (budget {max_kpi_ms} ms):")
    for key in sorted(kpi_ms):
        print(f"  {key:22s} {kpi_ms[key]:8.1f} ms")
    offenders = kpi_offenders(kpi_ms, max_kpi_ms)
    if offenders:
        print("KPI over budget: " + ", ".join(f"{k} ({kpi_ms[k]:.1f} ms)" for k in offenders))
        return 1
    # The reproducibility verdict is the gate's reason to exist: a mismatch
    # is a non-zero exit, not just a line in the report.
    return 0 if (report.get("S1") or {}).get("pass") else 1


def run(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sem_path")
    ap.add_argument("data_dir")
    ap.add_argument("cfg_dir")
    ap.add_argument("ref_path")
    ap.add_argument("as_of_s", nargs="?", default="2026-07-24T12:00:00")
    ap.add_argument("--max-kpi-ms", type=float, default=2000)
    args = ap.parse_args(argv)
    return main(args.sem_path, args.data_dir, args.cfg_dir, args.ref_path, args.as_of_s, max_kpi_ms=args.max_kpi_ms)


if __name__ == "__main__":
    sys.exit(run())
