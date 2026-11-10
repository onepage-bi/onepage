#!/usr/bin/env node
// Runs a generator bundle (engine contract R2) in Pyodide, but from the command
// line rather than from the browser worker. Needed to prove parity: the same
// bundle on the same files is run here (wasm32, CPython 3.14) and natively
// (CPython 3.13) — and the numbers are compared byte for byte.
//
// The semantic reference is static/js/gen-worker.js, steps 1-7. Everything done
// there because of pitfalls (the "service" disposition is kept until finish(), other
// files are deleted immediately, python-calamine is always loaded) is repeated here literally.
//
//   node tools/run_bundle_pyodide.mjs --bundle B.zip --files files.json --out DIR
//                                     [--pyodide DIR] [--schema SHA]
//
// node stdlib only: the script ships in the agent's offline kit, there must be no network.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..');

const INBOX = '/inbox';
const OUT_DIR = '/out';
const CACHE_DIR = '/cache';

function die(msg) {
  process.stderr.write('ERROR: ' + msg + '\n');
  process.exit(1);
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const k = argv[i];
    const v = argv[i + 1];
    if (!k.startsWith('--')) die(`unexpected argument: ${k}`);
    if (v === undefined) die(`key ${k} has no value`);
    out[k.slice(2)] = v;
  }
  return out;
}

// Generator progress frames: one line to stderr roughly every 20 frames,
// otherwise a two-minute run over 500 workbooks looks hung.
function makeProgress(state) {
  return (stage, done, total, msg) => {
    state.count++;
    if (state.count % 20 === 1) {
      process.stderr.write(`[progress] ${stage} ${done}/${total} ${msg == null ? '' : msg}\n`);
    }
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  for (const need of ['bundle', 'files', 'out']) {
    if (!args[need]) die(`--${need} is required`);
  }
  const bundlePath = path.resolve(args.bundle);
  const filesPath = path.resolve(args.files);
  const outDir = path.resolve(args.out);
  const pyodideDir = path.resolve(args.pyodide || path.join(REPO, 'static', 'pyodide'));
  const schemaVersion = args.schema || 'dev';

  if (!fs.existsSync(bundlePath)) die(`bundle not found: ${bundlePath}`);
  if (!fs.existsSync(pyodideDir)) die(`Pyodide directory not found: ${pyodideDir}`);

  // files.json: the array order is part of the contract (a generator may depend
  // on the add_file order), so nothing is sorted.
  let files;
  try {
    files = JSON.parse(fs.readFileSync(filesPath, 'utf8'));
  } catch (e) {
    die(`cannot read --files ${filesPath}: ${e.message}`);
  }
  if (!Array.isArray(files) || files.length === 0) die('--files must be a non-empty array');
  for (const f of files) {
    for (const k of ['name', 'mtime_ms', 'size', 'path']) {
      if (f[k] === undefined) die(`an entry in files.json has no field ${k}: ${JSON.stringify(f)}`);
    }
    if (!fs.existsSync(f.path)) die(`file not on disk: ${f.path}`);
  }

  fs.mkdirSync(outDir, { recursive: true });

  // ── 1. Boot Pyodide from the vendored directory ─────────────────────
  const baseUrl = pathToFileURL(pyodideDir + path.sep);
  const { loadPyodide } = await import(baseUrl + 'pyodide.mjs');
  // indexURL is a filesystem path, not a URL: under node loadPyodide reads wheels via fs.
  const pyodide = await loadPyodide({ indexURL: pyodideDir + path.sep });

  // ── 2. Dependencies: only from the local lock, no network ───────────
  const lockPath = path.join(pyodideDir, 'pyodide-lock.json');
  if (!fs.existsSync(lockPath)) die(`no pyodide-lock.json in ${pyodideDir}`);
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const have = new Set(Object.keys(lock.packages || {}));

  // The manifest is read BEFORE unpacking into the Pyodide FS — it is needed for the deps list.
  const manifest = await readManifestFromZip(pyodide, bundlePath);
  const deps = manifest.deps || [];
  const missing = deps.map(d => d.name).filter(n => !have.has(n));
  if (missing.length) {
    // Skipping silently would mean an obscure ImportError in the middle of the
    // run or, worse, a different result. Refuse, naming the package.
    die('not in the local pyodide-lock.json (no network, nothing to install from): ' + missing.join(', '));
  }
  // python-calamine is always loaded: it is the lineage probe's reader,
  // whether or not the generator declared it (as in gen-worker.js).
  const toLoad = Array.from(new Set(['python-calamine', ...deps.map(d => d.name)]));
  process.stderr.write(`[deps] ${toLoad.join(', ')}\n`);
  await pyodide.loadPackage(toLoad);

  // ── 3. Bundle into the Pyodide FS → /app → sys.path ─────────────────
  pyodide.FS.writeFile('/bundle.zip', new Uint8Array(fs.readFileSync(bundlePath)));
  await pyodide.runPythonAsync(`
import os, sys, zipfile
os.makedirs('/app', exist_ok=True)
os.makedirs('${INBOX}', exist_ok=True)
os.makedirs('${OUT_DIR}', exist_ok=True)
os.makedirs('${CACHE_DIR}', exist_ok=True)
with zipfile.ZipFile('/bundle.zip') as _z:
    _z.extractall('/app')
if '/app' not in sys.path:
    sys.path.insert(0, '/app')
`);

  // ── 4. Open the generator session ───────────────────────────────────
  const module = (manifest.entrypoint && manifest.entrypoint.module) || '';
  if (!module) die('manifest.json has no entrypoint.module');
  const pstate = { count: 0 };
  pyodide.globals.set('js_progress', makeProgress(pstate));
  pyodide.globals.set('gen_module', module);
  pyodide.globals.set('gen_out', OUT_DIR);
  pyodide.globals.set('gen_cache', CACHE_DIR);
  pyodide.globals.set('gen_schema', schemaVersion);
  const session = await pyodide.runPythonAsync(`
import importlib
_mod = importlib.import_module(gen_module)
_session = _mod.begin(gen_out, gen_cache, gen_schema, js_progress)
_session
`);

  // ── 5. Stream files from the host disk ──────────────────────────────
  const dispositions = [];
  const keepUntilFinish = [];
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (i % 20 === 0) process.stderr.write(`[files] ${i}/${files.length} ${f.name}\n`);
    const p = INBOX + '/' + f.name;
    pyodide.FS.writeFile(p, new Uint8Array(fs.readFileSync(f.path)));
    const kind = session.add_file(f.name, f.mtime_ms, f.size, p);
    dispositions.push({ name: f.name, kind });
    if (kind === 'service') {
      // Service workbooks (the reasons catalog etc.) are read only in finish() —
      // deleting them right away would break finish().
      keepUntilFinish.push(p);
    } else {
      pyodide.FS.unlink(p);
    }
  }

  // ── 6. finish() → outputs from /out to the host disk ────────────────
  const resultProxy = session.finish();
  const result = resultProxy.toJs({ dict_converter: Object.fromEntries });
  resultProxy.destroy();
  session.destroy();
  for (const p of keepUntilFinish) {
    try { pyodide.FS.unlink(p); } catch (e) { /* already deleted by the generator — fine */ }
  }

  const copyOut = (name) => {
    const u8 = pyodide.FS.readFile(OUT_DIR + '/' + name);
    fs.writeFileSync(path.join(outDir, name), Buffer.from(u8));
  };
  if (result.html) copyOut(result.html);
  const slots = result.slots || {};
  for (const idx of Object.keys(slots)) copyOut(slots[idx]);
  const writeBack = result.write_back || {};
  for (const idx of Object.keys(writeBack)) copyOut(writeBack[idx]);

  // ── 7. result.json ──────────────────────────────────────────────────
  // Versions and numpy's default integer width are mandatory: without them a
  // "wasm int32 vs native int64" divergence is indistinguishable from a version divergence.
  const versions = JSON.parse(await pyodide.runPythonAsync(`
import json, sys, platform
import numpy as np, pandas as pd
# python_calamine has no __version__ (checked: the 0.6.2 wheel does not provide it),
# so the version comes from the distribution metadata, not from a module attribute.
try:
    from importlib.metadata import version as _ver
    _cal = _ver('python-calamine')
except Exception:
    try:
        import python_calamine as _pc
        _cal = getattr(_pc, '__version__', None)
    except Exception:
        _cal = None
json.dumps({
    "python": platform.python_version(),
    "pandas": pd.__version__,
    "numpy": np.__version__,
    "calamine": _cal,
    "numpy_default_int": np.array([1]).dtype.name,
    "sys_maxsize": sys.maxsize,
})
`));

  const payload = {
    result: {
      html: result.html === undefined ? null : result.html,
      slots: slots,
      write_back: writeBack,
    },
    dispositions,
    progress_count: pstate.count,
    versions,
  };
  fs.writeFileSync(path.join(outDir, 'result.json'), JSON.stringify(payload, null, 2) + '\n');
  process.stderr.write(`[done] outputs in ${outDir}\n`);
}

// manifest.json lives inside the zip; there is no need to unpack the whole archive
// to the host disk for it — read it with the same zipfile inside Pyodide (stdlib, no
// dependency on node zip libraries, which the offline kit does not have).
async function readManifestFromZip(pyodide, bundlePath) {
  pyodide.FS.writeFile('/manifest-probe.zip', new Uint8Array(fs.readFileSync(bundlePath)));
  const txt = await pyodide.runPythonAsync(`
import zipfile
with zipfile.ZipFile('/manifest-probe.zip') as _z:
    _m = _z.read('manifest.json').decode('utf-8')
_m
`);
  pyodide.FS.unlink('/manifest-probe.zip');
  return JSON.parse(txt);
}

main().catch((e) => {
  process.stderr.write('ERROR: ' + (e && e.stack ? e.stack : String(e)) + '\n');
  process.exit(1);
});
