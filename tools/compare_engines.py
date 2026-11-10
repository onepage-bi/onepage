#!/usr/bin/env python3
"""Column-by-column equivalence check: shipped /explore engine vs engine v2.

    .venv/bin/python tools/compare_engines.py --dir ~/data/SL \
        --explore /path/to/_explore.py

Indexes the same files through both engines and prints every column whose NAME
or TYPE differs. This is the gate that matters for the migration: /explore's
query builder, the semantic layer and every saved query write SQL against these
names and types, so a rewrite that is fast and *different* is a regression, not
an improvement.

Needs node (for the v2 side) and the v1 dependencies (pandas, duckdb,
python-calamine) in the running interpreter.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

V2_SCRIPT = r"""
import { createEngine } from '../static/js/explore2/engine.js';
import { readFile } from 'node:fs/promises';
const [dir, sheet, table, header, ...files] = process.argv.slice(2);
const eng = await createEngine({ base: new URL('../static/', import.meta.url).href });
for (const f of files) {
  await eng.indexFile(f, await readFile(`${dir}/${f}`),
    { table, partition: f.replace(/\.xlsx?$/i, ''), sheet, headerRow: Number(header) });
}
const d = await eng.raw(`DESCRIBE SELECT * FROM ${table}`);
console.log('@@' + JSON.stringify(d.toArray().map(r => {
  const j = r.toJSON();
  return [String(j.column_name), String(j.column_type)];
})));
await eng.close();
process.exit(0);
"""


def load_explore(path: Path):
    spec = importlib.util.spec_from_file_location("_explore_cmp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.expanduser("~/data/SL"))
    ap.add_argument("--sheet", default="Заказы")
    ap.add_argument("--table", default="zakazy")
    ap.add_argument("--header", type=int, default=0)
    ap.add_argument("--explore", required=True)
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.dir)
                   if f.lower().endswith((".xlsx", ".xls")) and not f.startswith("~$"))

    # ── v2 (node) ────────────────────────────────────────────────────────
    tmp = ROOT / "tools" / f"_cmp_{os.getpid()}.mjs"
    tmp.write_text(V2_SCRIPT)
    try:
        proc = subprocess.run(
            ["node", str(tmp), args.dir, args.sheet, args.table, str(args.header), *files],
            capture_output=True, text=True, cwd=ROOT)
    finally:
        tmp.unlink(missing_ok=True)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("@@")), None)
    if line is None:
        print(proc.stdout[-2000:], proc.stderr[-2000:], file=sys.stderr)
        return 1
    v2 = dict(json.loads(line[2:]))

    # ── v1 (CPython) ─────────────────────────────────────────────────────
    ex = load_explore(Path(args.explore))
    inbox = Path(tempfile.mkdtemp(prefix="op-cmp-"))
    try:
        for f in files:
            shutil.copyfile(Path(args.dir) / f, inbox / f)
            ex.index_file(str(inbox / f), sheet=args.sheet, header_row=args.header,
                          table=args.table, partition=f.rsplit(".", 1)[0], file=f)
    finally:
        shutil.rmtree(inbox, ignore_errors=True)
    con = ex._conn()
    v1 = {r[0]: str(r[1]).upper()
          for r in con.execute(f'DESCRIBE SELECT * FROM "{args.table}"').fetchall()}

    # ── diff ─────────────────────────────────────────────────────────────
    only1 = [c for c in v1 if c not in v2]
    only2 = [c for c in v2 if c not in v1]
    both = [c for c in v1 if c in v2]
    difftypes = [(c, v1[c], v2[c]) for c in both if v1[c] != v2[c]]

    print(f"v1 columns: {len(v1)}   v2 columns: {len(v2)}   shared: {len(both)}")
    if only1:
        print(f"\nONLY IN v1 ({len(only1)}):")
        for c in only1:
            print(f"   {c}  [{v1[c]}]")
    if only2:
        print(f"\nONLY IN v2 ({len(only2)}):")
        for c in only2:
            print(f"   {c}  [{v2[c]}]")
    if difftypes:
        print(f"\nTYPE DIFFERS ({len(difftypes)}):")
        for c, a, b in difftypes:
            print(f"   {c:<45} v1={a:<14} v2={b}")
    if not (only1 or only2 or difftypes):
        print("\nIDENTICAL schema — names and types agree column for column.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
