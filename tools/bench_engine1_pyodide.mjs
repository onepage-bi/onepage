#!/usr/bin/env node
/**
 * The apples-to-apples half of the comparison: the CURRENT engine's parse step
 * running under Pyodide, i.e. the wasm CPython a /explore user actually gets.
 *
 *   node tools/bench_engine1_pyodide.mjs [--dir ~/data/SL]
 *
 * Prerequisite: `python3 deploy/fetch-pyodide.py` (populates static/pyodide/,
 * gitignored, ~21 MB).
 *
 * WHAT THIS MEASURES, AND WHAT IT DOES NOT
 * ----------------------------------------
 * It runs the expensive half of `_explore.index_file`: `pd.read_excel(engine=
 * 'calamine')` plus verbatim copies of `_norm_col` / `_frame_columns` /
 * `_coerce_time_cols` / `_coerce_mixed_cols`. That is where the current
 * engine's index time goes.
 *
 * It does NOT run the DuckDB half, because **DuckDB is not in the vendored
 * Pyodide subset** (`deploy/fetch-pyodide.py` packages micropip, pandas,
 * python-calamine and xlrd — no duckdb). So this number is the current
 * engine's parse cost in the browser, not its full index cost; the real
 * browser index time is this plus a `con.register()` + view rebuild.
 *
 * Compare against the `parse` column of `tools/bench_engine2.mjs`, which is
 * the exactly-corresponding step of the new engine.
 */
import { readFileSync } from 'node:fs';
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const ROOT = new URL('../', import.meta.url);

function arg(name, dflt) {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}
const DIR = arg('dir', `${process.env.HOME}/Downloads/Onepage/SL`);
const SHEET = arg('sheet', 'Заказы');

// Verbatim from _explore.py — copied rather than imported so this script needs
// nothing but the Pyodide subset.
const PY = String.raw`
import io, re, time, unicodedata, datetime as _dt
import pandas as pd

_UNNAMED_RE = re.compile(r"^unnamed:\s*\d+(\.\d+)?$")
_DEDUPE_SUFFIX_RE = re.compile(r"\.\d+$")
_WS_RE = re.compile(r"\s+")
PARTITION_COL, FILE_COL = "__partition", "__file"

def _norm_col(raw):
    if not isinstance(raw, str):
        return None
    key = _WS_RE.sub(" ", unicodedata.normalize("NFC", raw).strip().casefold())
    if not key or _UNNAMED_RE.match(key):
        return None
    key = _DEDUPE_SUFFIX_RE.sub("", key).strip()
    return key or None

def _frame_columns(df):
    used = {PARTITION_COL, FILE_COL}
    out = []
    for i, raw in enumerate(list(df.columns)):
        key = _norm_col(raw if isinstance(raw, str) else str(raw))
        if not key:
            key = f"col_{i + 1}"
        if key in used:
            n = 2
            while f"{key}__{n}" in used:
                n += 1
            key = f"{key}__{n}"
        used.add(key)
        out.append(key)
    df.columns = out
    return out

def _coerce_time_cols(df):
    for col in list(df.columns):
        s = df[col]
        if s.dtype != object:
            continue
        if s.map(lambda v: isinstance(v, _dt.time)).any():
            df[col] = s.map(lambda v: v.isoformat() if isinstance(v, _dt.time) else v)

def _coerce_mixed_cols(df):
    for col in list(df.columns):
        s = df[col]
        if s.dtype != object:
            continue
        kinds = set()
        for v in s:
            if v is None or (isinstance(v, float) and v != v):
                continue
            if isinstance(v, str):
                kinds.add("blank" if not v.strip() else "str")
            elif isinstance(v, bool):
                kinds.add("str")
            elif isinstance(v, (int, float)):
                kinds.add("num")
            else:
                kinds.add("other")
            if {"str", "num"} <= kinds:
                break
        if "blank" in kinds:
            s = s.map(lambda v: None if isinstance(v, str) and not v.strip() else v)
        if "num" in kinds and "str" not in kinds and "other" not in kinds:
            df[col] = pd.to_numeric(s, errors="coerce")
        elif "num" in kinds or ("blank" in kinds and "other" in kinds):
            df[col] = s.map(lambda v: None if v is None or (isinstance(v, float) and v != v) else str(v))
        elif "blank" in kinds:
            df[col] = s

def parse(path, sheet):
    t0 = time.perf_counter()
    df = pd.read_excel(path, sheet_name=sheet, header=0, engine="calamine")
    cols = _frame_columns(df)
    _coerce_time_cols(df)
    _coerce_mixed_cols(df)
    df[PARTITION_COL] = "p"
    df[FILE_COL] = "f"
    ms = (time.perf_counter() - t0) * 1000
    try:
        b = int(df.memory_usage(deep=True).sum())
    except Exception:
        b = 0
    return [ms, len(df), len(cols), b]
`;

async function main() {
  const pyodideDir = fileURLToPath(new URL('static/pyodide/', ROOT));
  const { loadPyodide } = await import(new URL('static/pyodide/pyodide.mjs', ROOT).href);

  const t0 = Date.now();
  const py = await loadPyodide({ indexURL: pyodideDir });
  await py.loadPackage(['pandas', 'python-calamine'], { messageCallback: () => {} });
  const bootMs = Date.now() - t0;
  console.log(`engine v1 under Pyodide  boot+packages=${bootMs} ms`);
  console.log(`python ${py.runPython('import sys; sys.version.split()[0]')}  ` +
    `pandas ${py.runPython('import pandas; pandas.__version__')}`);
  console.log(`dir=${DIR}  sheet=${SHEET}\n`);

  py.runPython(PY);
  const parse = py.globals.get('parse');

  const files = readdirSync(DIR).filter(f => /\.xlsx?$/i.test(f) && !f.startsWith('~$')).sort();
  py.FS.mkdir('/inbox');
  console.log('file                 bytes    rows   cols  parse_ms   MB/s   frame_MB');
  console.log('---------------------------------------------------------------------');
  let tot = 0, totRows = 0, totBytes = 0;
  for (const f of files) {
    const bytes = readFileSync(`${DIR}/${f}`);
    py.FS.writeFile(`/inbox/${f}`, bytes);
    const r = parse(`/inbox/${f}`, SHEET).toJs();
    py.FS.unlink(`/inbox/${f}`);
    const [ms, rows, cols, frameBytes] = r;
    tot += ms; totRows += rows; totBytes += bytes.length;
    console.log(`${f.padEnd(20)} ${String(bytes.length).padStart(8)} ${String(rows).padStart(6)} ` +
      `${String(cols).padStart(6)} ${ms.toFixed(1).padStart(9)} ` +
      `${(bytes.length / 1048576 / (ms / 1000)).toFixed(1).padStart(6)} ` +
      `${(frameBytes / 1048576).toFixed(1).padStart(10)}`);
  }
  console.log('---------------------------------------------------------------------');
  console.log(`TOTAL                ${String(totBytes).padStart(8)} ${String(totRows).padStart(6)}` +
    `        ${tot.toFixed(1).padStart(9)}`);
  console.log(`\nparse-only wall-clock: ${tot.toFixed(1)} ms for ${totRows} rows / ` +
    `${(totBytes / 1048576).toFixed(1)} MB  (DuckDB half NOT included — see header)`);
  console.log(`node rss: ${(process.memoryUsage().rss / 1048576).toFixed(0)} MB ` +
    `(most of it the Pyodide heap)`);
  process.exit(0);
}

main().catch(e => { console.error(e); process.exit(1); });
