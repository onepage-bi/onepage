/* OnePage — browser generator engine (Worker with Pyodide).
 * RFC docs/RFC_GENERATORS_RESEARCH.md Appendix D (stage 3).
 *
 * The ONLY place Python runs. Boots Pyodide, installs the manifest's deps,
 * unpacks the immutable bundle, mounts a persistent OPFS aggregate cache
 * (D.2), then streams the user's files one at a time into the generator's
 * begin()/add_file()/finish() session (D.1 — O(1) memory), and ships the
 * result back as transferable ArrayBuffers.
 *
 * Contract with the generator (Python side, refactored in parallel):
 *   mod = importlib.import_module(manifest["entrypoint"]["module"])
 *   session = mod.begin(out_dir, cache_dir, schema_version, progress)
 *   session.add_file(name, mtime_ms, size, path)   # file already at `path`
 *   result = session.finish()
 *   result = {"html": "dashboard.html"|None,
 *             "slots": {"0": "sl_bc_data.bin.gz", ...},
 *             "write_back": {"1": "..."} | {}}      # names relative to out_dir
 *
 * No performance.mark here — perf.js runs in the page. Stage timings ride
 * postMessage; the main thread places the marks/measures.
 *
 * Data Lineage v2 (docs/RFC plan, Phase B): alongside the parse loop, a
 * separate probe (static/py/_lineage.py) sniffs each workbook's structure
 * and records which sheets/columns the generator actually opened. Purely
 * observational — every probe call is try/caught, because a lineage bug
 * must never turn a working report run into a failed one. Results ride
 * back as {type:'observe'} batches, independent of the 'progress' stream.
 */

// ── t() inside a Worker ──────────────────────────────────────────────────
// A Worker has no `window`, so the page's global t() is out of reach, and the
// catalogue is not loaded here. This shim keeps the call shape identical to
// the rest of the codebase — the strings below ARE catalogue keys and the
// extractor collects them — but it always returns the English source (its `{ctx:N}`
// disambiguator stripped, as the page's t() does) with its parameters filled in. Making them actually translate needs the refactor from
// the i18n design §3: the worker returns a CODE, the main thread renders the
// text with the real t(). Until then these particular messages stay English in
// every language, which is the same graceful degradation as an untranslated key.
function t(key, params) {
  key = String(key).replace(/\s*\{ctx:[^}]*\}/, '');
  if (!params) return key;
  return key.replace(/\{(\w+)\}/g, function (m, name) {
    var v = params[name];
    return v === undefined || v === null ? m : String(v);
  });
}
'use strict';

const OUT_DIR = '/out';
const CACHE_DIR = '/cache';
async function fileHandleAt(dir, relPath) {
  const parts = String(relPath).split('/').filter(Boolean);
  let d = dir;
  for (let i = 0; i < parts.length - 1; i++) d = await d.getDirectoryHandle(parts[i]);
  return d.getFileHandle(parts[parts.length - 1]);
}
const INBOX = '/inbox';

// Copy a File into the Pyodide FS in 8 MB slices. `file.arrayBuffer()` +
// `FS.writeFile(new Uint8Array(buf))` held TWO full copies of the file in the
// worker at once (the ArrayBuffer and the MEMFS node); for a 300 MB CSV that
// was 600 MB before Python read a byte. Now the JS side never holds more
// than one slice; MEMFS keeps the single copy add_file reads from.
const FS_CHUNK = 8 * 1024 * 1024;
async function streamIntoFS(pyodide, path, file) {
  const stream = pyodide.FS.open(path, 'w');
  try {
    let pos = 0;
    while (pos < file.size) {
      const end = Math.min(pos + FS_CHUNK, file.size);
      const u8 = new Uint8Array(await file.slice(pos, end).arrayBuffer());
      pyodide.FS.write(stream, u8, 0, u8.length, pos);
      pos = end;
    }
  } finally {
    pyodide.FS.close(stream);
  }
}
const WASM_WARN_BYTES = 2.4 * (2 ** 30);   // ~2.4 GiB — approaching the 2.75 GB wall (D.3)
const OBSERVE_BATCH_SIZE = 25;
const MAX_STAGES = 32;                          // mirrors _lineage.MAX_STAGES / the server's ingest cap
const OBSERVE_BUDGET_BYTES = 2 * 1024 * 1024;   // sniff schemas (+ checks/profiles) cap out at ~2 MB serialised
const MAX_REASON_CHARS = 200;                   // mirrors _lineage.MAX_REASON_CHARS: schema error/reason text
const CHECK_BUDGET_MS = 20000;   // wall-clock spent inside run_checks() across the whole run

// UTF-8 byte length of a string, NOT String.length (which counts UTF-16 code
// units) — the server's own budget check (_MAX_OBS_BODY in
// app/api/generators.py) is on real UTF-8 bytes, and every non-ASCII
// character (Cyrillic sheet/column names, known_values samples) is 2 bytes
// there but only 1 code unit here. Using .length under-counts by roughly 2x
// for the Russian-heavy payloads this budget exists to bound.
function byteLength(s) {
  return new TextEncoder().encode(s).length;
}

// Pure — no Pyodide/DOM — so a node harness can exercise them directly
// (tests/checks_filter.js). Exported at the bottom of this file when running
// under node; the worker itself just calls them inline.

// Data quality (P1): the manifest's checks scoped to ONE file, by the same
// `match` glob the engine already recorded on the file entry (`matched_input`,
// set from `inputsDecl` during the folder scan — see gen-engine.js). A file
// the allow-list never matched (`matched_input` unset) gets no checks, same
// as it gets no declared columns below.
function checksFor(checks, matchedInput) {
  return (checks || []).filter(c => c && c.match === matchedInput);
}

// The declared columns for ONE file's match, reshaped from `inputs[].sheets`
// (list of {name: sheet_glob, columns: [...]}) into the {sheet_glob: [column,
// ...]} map `run_checks` profiles against — the same shape `sniff`'s caller
// never sees, because this one is engine-internal only.
function declaredFor(inputs, matchedInput) {
  const declared = {};
  if (!matchedInput) return declared;
  const input = (inputs || []).find(i => i && i.match === matchedInput);
  if (!input) return declared;
  for (const sh of (input.sheets || [])) {
    if (!sh || !Array.isArray(sh.columns) || !sh.columns.length) continue;
    declared[sh.name || '*'] = sh.columns;
  }
  return declared;
}

// Pipeline stages (P4.1): the generator narrates its own steps through
// `onepage.stage(name, df)`; the probe accumulates the snapshots and hands
// them over at the end of the run. Both helpers below are pure so a node
// harness can drive them (tests/stages_flush.js).

// Append `incoming` to `existing` up to `max` — the same 32-stage ceiling the
// probe and the server's ingest enforce, applied again here because a stubbed
// or future probe is not a trusted source.
function takeStages(existing, incoming, max) {
  for (const st of (incoming || [])) {
    if (!st || typeof st !== 'object') continue;
    if (existing.length >= max) break;
    existing.push(st);
  }
  return existing;
}

// Same as takeStages, but ALSO charges each stage's serialised size against
// `budgetLeft` — the STAGE CONTRACT says stage size "counts toward existing
// check_manifest_size style limits", and stages otherwise ride into the
// FINAL observe batch with no byte accounting at all (files have observeBytes/
// OBSERVE_BUDGET_BYTES; stages had nothing, so a wide/many-column pipeline
// could blow the server's 2 MB body cap and drop the whole final batch —
// taking `final:true`, and observations.finalize(), with it). A stage that
// doesn't fit whole is retried with `profiles` stripped (the heaviest field,
// the same trade the 413-recovery path already makes) before being left out;
// once one stage is left out for size, every later one is too — later stages
// are never smaller. Returns {spent, truncated} so the caller's own running
// total (observeBytes) stays truthful.
function takeStagesBudgeted(existing, incoming, max, budgetLeft) {
  let spent = 0;
  let truncated = false;
  const cap = (typeof budgetLeft === 'number' && budgetLeft > 0) ? budgetLeft : 0;
  for (const st of (incoming || [])) {
    if (!st || typeof st !== 'object') continue;
    if (existing.length >= max) break;
    let entry = st;
    let size = byteLength(JSON.stringify(st));
    if (spent + size > cap) {
      const stripped = Object.assign({}, st, { profiles: [] });
      const strippedSize = byteLength(JSON.stringify(stripped));
      if (spent + strippedSize > cap) { truncated = true; break; }
      entry = stripped;
      size = strippedSize;
    }
    existing.push(entry);
    spent += size;
  }
  return { spent, truncated };
}

// The 'observe' message body. Stages ride ONLY on the final flush (they
// describe the run, not a file, and the server replaces the whole set per
// run); a flush with neither files nor stages is not worth a message.
function observePayload(files, stages, budget) {
  if (!files.length && !(stages && stages.length)) return null;
  const payload = { type: 'observe', files: files };
  if (stages && stages.length) payload.stages = stages;
  if (budget) payload.budget = true;
  return payload;
}

function progress(stage, done, total, msg) {
  self.postMessage({ type: 'progress', stage: stage, done: done, total: total, msg: msg || '' });
}

// Pyodide surfaces Python exceptions as JS Errors whose .message is the full
// traceback. Short line for the human, whole thing for the detail column.
function shortMsg(err) {
  const full = String((err && err.message) || err || 'error');
  const lines = full.split('\n').map(l => l.trim()).filter(Boolean);
  return (lines.length ? lines[lines.length - 1] : full).slice(0, 300);
}

function wasmBytes(pyodide) {
  try { return pyodide._module.HEAPU8.buffer.byteLength || 0; } catch (e) { return 0; }
}

// Guarded: this file loads under node too (tests/checks_filter.js exercises
// the pure functions above via `require`), and `self` is a Worker/browser
// global that doesn't exist there.
if (typeof self !== 'undefined') {
  self.onmessage = async (e) => {
    const msg = e.data;
    if (!msg || !msg.type) return;
    // Two disjoint modes share this worker: the generator RUN path (below)
    // and /explore (exploreMessage). They share the boot code and nothing
    // else — no manifest, no bundle, no generator module on the explore side
    // — so a change to one cannot alter the other's behaviour.
    if (msg.type.slice(0, 8) === 'explore:') {
      try {
        await exploreMessage(msg);
      } catch (err) {
        self.postMessage({ type: 'explore:error', id: msg.id || null, message: shortMsg(err),
          detail: String((err && err.message) || err || '').slice(0, 20000) });
      }
      return;
    }
    if (msg.type !== 'start') return;
    try {
      await run(msg);
    } catch (err) {
      self.postMessage({ type: 'error', message: shortMsg(err), detail: String((err && err.message) || err || '').slice(0, 20000) });
    }
  };
}

async function run(opts) {
  // dirHandles[0] is the main folder; extras follow in list order. Each file's
  // `dir` indexes into this array (0 when unset — the single-folder case).
  const {
    dirHandles, files, auxFiles = [], manifest, bundleUrl, pyodideBase, schemaVersion, generatorId,
    inputs = [], lastKnown = {}, forceDeps = [], checks = [],
  } = opts;

  // ── Lineage observation state ───────────────────────────────────────
  // Batches ride independently of 'progress'/'done'; the page (B1) merges
  // them into source_observations as they arrive. The contract requires the
  // final partial batch to be flushed BEFORE 'done' and BEFORE any rethrown
  // error, so the whole body below runs inside a try/catch that flushes.
  let observeBytes = 0, filesSniffed = 0, sniffMs = 0, sniffFailures = 0, sniffWarned = false;
  let budgetExceeded = false;
  let filesChecked = 0, checkMs = 0, checkFailures = 0, checkWarned = false;
  let checkBudgetExceeded = false;
  let obsBatch = [];
  let obsBatchBudget = false;
  // Pipeline stages (P4.1): whatever the generator narrated through
  // onepage.stage(). Drained once, at the final flush — they describe the run
  // as a whole, not one file, and the server replaces the whole set per run.
  let lineage = null;
  let stagesOut = [];

  function collectStages() {
    if (!lineage) return;
    try {
      const proxy = lineage.drain_stages();
      const list = proxy.toJs({ dict_converter: Object.fromEntries });
      proxy.destroy();
      // Same budget files already spend from (observeBytes/OBSERVE_BUDGET_BYTES)
      // — stages are the last thing added to the final batch, so whatever
      // schemas/checks/profiles already cost this run is exactly what's left
      // for them to work with. A run that already spent the whole budget on
      // file schemas gets no stages at all rather than risk the final send.
      const { spent, truncated } = takeStagesBudgeted(
        stagesOut, list, MAX_STAGES, OBSERVE_BUDGET_BYTES - observeBytes);
      observeBytes += spent;
      if (truncated) obsBatchBudget = true;
    } catch (e) {
      // An older/stubbed probe without drain_stages — stages are a bonus, not
      // a contract with the generator; never worth a warning.
    }
  }

  function flushObserve(final) {
    if (final) collectStages();
    const payload = observePayload(obsBatch, final ? stagesOut : null, obsBatchBudget);
    if (!payload) return;
    self.postMessage(payload);
    obsBatch = [];
    obsBatchBudget = false;
  }

  function pushObserve(entry) {
    obsBatch.push(entry);
    if (obsBatch.length >= OBSERVE_BATCH_SIZE) flushObserve(false);
  }

  // A stub/broken _lineage.py must not fail the run — count and warn once,
  // keep going. This covers both sniff() (per file) and any surprise from
  // install_hooks() itself (caught separately, at the probe-load site).
  function warnSniffFailure(e) {
    sniffFailures++;
    if (!sniffWarned) {
      sniffWarned = true;
      self.postMessage({ type: 'warning', msg: 'Lineage probe failing on some files — continuing without those schemas: ' + shortMsg(e) });
    }
  }

  // Same discipline as warnSniffFailure, for run_checks(): one bad workbook
  // (unreadable, a pathological pattern) must not stop checks for the rest
  // of the run — count it and warn once.
  function warnCheckFailure(e) {
    checkFailures++;
    if (!checkWarned) {
      checkWarned = true;
      self.postMessage({ type: 'warning', msg: 'Quality checks failing on some files — continuing without those results: ' + shortMsg(e) });
    }
  }

  try {
    // ── 1. Boot Pyodide ────────────────────────────────────────────────
    // Module worker on purpose: Pyodide 314 throws "Classic web workers are
    // not supported" under importScripts — the runtime is ESM-only in workers.
    // (Page-context <script src=pyodide.js> still works, which is why the
    // boot microtest didn't catch this.) gen-engine.js spawns this worker
    // with {type: 'module'}.
    progress('boot', 0, 1, 'Loading Pyodide');
    const { loadPyodide } = await import(pyodideBase + 'pyodide.mjs');
    const pyodide = await loadPyodide({ indexURL: pyodideBase });

    // ── 2. Dependencies (server pre-computed wasm32 status per dep) ─────
    progress('deps', 0, 1, 'Resolving dependencies');
    const deps = manifest.deps || [];
    const bundled = deps.filter(d => d.wasm32 === 'bundled').map(d => d.name);
    const pypi = deps.filter(d => d.wasm32 === 'pypi-wheel' || d.wasm32 === 'pypi-pure');
    const blocked = deps.filter(d => d.wasm32 === 'unavailable' || d.wasm32 === 'unknown');
    if (blocked.length) {
      // The server border should have kept these off the browser engine; if one
      // slips through, fail with a message that names the culprit.
      throw new Error('Dependency without a WebAssembly build: '
        + blocked.map(d => `${d.name} (${d.wasm32})`).join(', ')
        + ' — this generator must run on the agent engine.');
    }
    // python-calamine loads unconditionally — it is the lineage probe's
    // reader (sniff() + install_hooks()), independent of whether the
    // generator itself declares it as a dep. It ships in the self-hosted
    // bundle (deploy/fetch-pyodide.py PACKAGES), so this never hits network.
    // De-duped against `bundled`/`forceDeps` so a generator that already
    // lists it (or a caller that force-loads it for a test) doesn't double-load.
    const toLoad = Array.from(new Set(['python-calamine', ...bundled, ...(forceDeps || [])]));
    await pyodide.loadPackage(toLoad);
    if (pypi.length) {
      await pyodide.loadPackage('micropip');
      const micropip = pyodide.pyimport('micropip');
      // Same-origin first: the frozen manifest carries each PyPI dep's wheel
      // filename, and a self-hosted deployment mirrors those wheels under
      // {base}wheels/ (deploy/fetch-pyodide.py). Corporate proxies/CSP may
      // block pypi.org from the page origin — the external fetch is only a
      // fallback.
      const specs = [];
      for (const d of pypi) {
        let spec = `${d.name}==${d.version}`;
        if (d.wheel) {
          const local = new URL(pyodideBase + 'wheels/' + d.wheel, self.location.href).href;
          try {
            const head = await fetch(local, { method: 'HEAD' });
            if (head.ok) spec = local;
          } catch (e) { /* keep the PyPI spec */ }
        }
        specs.push(spec);
      }
      await micropip.install(specs);
      micropip.destroy();
    }

    // ── 3. Bundle: fetch → FS → unzip → sys.path ───────────────────────
    progress('bundle', 0, 1, 'Fetching generator bundle');
    const bundleRes = await fetch(bundleUrl);
    if (!bundleRes.ok) throw new Error(`Bundle download failed: HTTP ${bundleRes.status}`);
    const bundleBuf = await bundleRes.arrayBuffer();
    pyodide.FS.writeFile('/bundle.zip', new Uint8Array(bundleBuf));
    await pyodide.runPythonAsync(`
import os, sys, zipfile
os.makedirs('/app', exist_ok=True)
os.makedirs('${INBOX}', exist_ok=True)
os.makedirs('${OUT_DIR}', exist_ok=True)
with zipfile.ZipFile('/bundle.zip') as _z:
    _z.extractall('/app')
if '/app' not in sys.path:
    sys.path.insert(0, '/app')
`);

    // ── 4. OPFS aggregate cache (D.2), mounted for the whole session ───
    let mountInfo = null;
    try {
      const opfsRoot = await navigator.storage.getDirectory();
      const parent = await opfsRoot.getDirectoryHandle('op-gen-cache', { create: true });
      const cacheHandle = await parent.getDirectoryHandle('g' + generatorId, { create: true });
      mountInfo = await pyodide.mountNativeFS(CACHE_DIR, cacheHandle);
    } catch (e) {
      // No OPFS / quota trouble at mount time: run without the warm cache rather
      // than crash. Correctness is unaffected — only cold-run speed (D.2).
      pyodide.FS.mkdirTree(CACHE_DIR);
      self.postMessage({ type: 'warning', msg: 'Cache unavailable — running without the warm cache.' });
    }

    // ── 4b. Lineage probe (static/py/_lineage.py) ───────────────────────
    // Must be imported and hooked BEFORE the generator module (step 5) so
    // `from python_calamine import CalamineWorkbook` in the generator binds
    // the wrapped class, not the original. Written into the Pyodide FS
    // (rather than pyodide.runPython(src) from a string) so tracebacks name
    // the real file instead of "<string>".
    try {
      const lineageUrl = new URL('/static/py/_lineage.py', self.location.href).href;
      const lineageRes = await fetch(lineageUrl);
      if (!lineageRes.ok) throw new Error(`HTTP ${lineageRes.status}`);
      const lineageSrc = await lineageRes.text();
      pyodide.FS.mkdirTree('/lib');
      pyodide.FS.writeFile('/lib/_lineage.py', lineageSrc);
      await pyodide.runPythonAsync(`
import sys
if '/lib' not in sys.path:
    sys.path.insert(0, '/lib')
import _lineage
`);
      lineage = pyodide.pyimport('_lineage');
      lineage.install_hooks();
    } catch (e) {
      // Probe failure is observability lost, not a run failure — the report
      // still generates from unhooked calamine/pandas.
      console.warn('Lineage probe unavailable:', e);
      self.postMessage({ type: 'warning', msg: 'Lineage probe unavailable — file observations disabled for this run: ' + shortMsg(e) });
      lineage = null;
    }

    // ── 5. Open the generator session ──────────────────────────────────
    const module = (manifest.entrypoint && manifest.entrypoint.module) || '';
    pyodide.globals.set('js_progress', progress);
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

    // ── 6. Per-file streaming loop (D.1 — O(1) memory) ─────────────────
    // add_file returns the file's disposition (engine contract): "service"
    // workbooks (e.g. the reasons catalog) must STAY on disk until finish()
    // reads them; "cached"/"parsed" files are dropped immediately.
    let peak = 0, filesParsed = 0, filesCached = 0;
    const keepUntilFinish = [];

    // Aux inputs arrive as transferred ArrayBuffers (already read in the page);
    // stream them through the same add_file disposition contract before the
    // main folder files. O(1): the buffer reference is dropped after writing.
    // Aux files are not observed (they are strict-named support workbooks
    // outside the manifest's `inputs`, not the analyst's declared data set).
    const auxTotal = auxFiles.length + files.length;
    const unreadable = [];        // files the folder stopped handing over mid-run
    const required = (manifest.required_files || []);
    for (let i = 0; i < auxFiles.length; i++) {
      const a = auxFiles[i];
      progress('files', i, auxTotal, a.name);
      const path = INBOX + '/' + a.name;
      pyodide.FS.writeFile(path, new Uint8Array(a.buf));
      a.buf = null;
      const kind = session.add_file(a.name, a.mtime_ms, a.size, path);
      if (kind === 'service') {
        keepUntilFinish.push(path);
      } else {
        pyodide.FS.unlink(path);
        if (kind === 'cached') filesCached++; else filesParsed++;
      }
      peak = Math.max(peak, wasmBytes(pyodide));
    }

    const checkArgMemo = new Map();
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      // Offset by the aux files already streamed so the X/Y counter is monotonic
      // across both loops (identical to the old call when auxFiles is empty).
      progress('files', auxFiles.length + i, auxTotal, f.name);
      // The handle was acquired during the folder scan, which on a long run was
      // minutes ago. Excel does not save in place — it writes a temp file and
      // renames it over the original — so a workbook someone touches mid-run
      // leaves a stale handle (NotFoundError / NotReadableError). Retry twice:
      // the rename window is short and a re-acquired handle usually reads fine.
      const path = INBOX + '/' + f.name;
      let readErr = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          // `path` (set for files from subfolders) is the real location;
          // `name` is the flat, prefixed name the generator sees.
          const fh = await fileHandleAt(dirHandles[f.dir || 0], f.path || f.name);
          const file = await fh.getFile();
          await streamIntoFS(pyodide, path, file);
          readErr = null;
          break;
        } catch (e) {
          readErr = e;
          try { pyodide.FS.unlink(path); } catch (_) { /* nothing written yet */ }
          if (attempt < 2) await new Promise(r => setTimeout(r, 250 * (attempt + 1)));
        }
      }
      if (readErr !== null) {
        // A file the manifest declares as required is load-bearing by the
        // author's own statement: dropping it would publish a report the
        // generator itself calls invalid, so this one still fails the run.
        if (required.indexOf(f.name) !== -1) throw readErr;
        // Otherwise: one file must not cost a two-minute run. The generator
        // decides what a file is worth only in add_file, which never gets to
        // run for a workbook we cannot read. Skipping it is visible — a warning
        // now, and the name in the done gauges — so a thinned dataset is never
        // mistaken for a complete one.
        unreadable.push({ name: f.name, error: readErr.name || String(readErr) });
        self.postMessage({ type: 'warning', msg: t('File skipped: {p1} ({p2}) — changed or disappeared during run.', {p1: f.name, p2: (readErr.name || t('read error'))}) });
        pushObserve({ dir: f.dir || 0, name: f.name, kind: 'unreadable' });
        continue;
      }
      // Skip re-sniffing a file whose size+mtime match the caller's last known
      // observation (page-supplied `lastKnown`) — its schema hasn't changed.
      const known = lastKnown[f.name];
      const unchanged = !!(known && known[0] === f.size && known[1] === f.mtime_ms);
      // `undefined` is the ONLY value that means "unchanged, keep the previous
      // observation" — the server's apply_batch stores schema_json only when
      // the key is present and non-null, so a null and a missing key are the
      // same row: NULL, which lineage walks back through the chain as
      // "unchanged". Every OTHER outcome therefore has to send a real object
      // carrying its reason, or the file ends up with no schema anywhere and
      // nothing to explain it (the production defect this fixes).
      let schema;
      if (unchanged) {
        schema = undefined;   // caller keeps the previous observation
      } else if (!lineage) {
        schema = { sheets: [], error: 'lineage probe unavailable' };
      } else if (budgetExceeded) {
        schema = { sheets: [], error: 'observe budget exceeded — schema not read' };
        obsBatchBudget = true;
      } else {
        try {
          const t0 = performance.now();
          const schemaProxy = lineage.sniff(path);
          sniffMs += performance.now() - t0;
          try {
            schema = schemaProxy.toJs({ dict_converter: Object.fromEntries });
          } finally {
            schemaProxy.destroy();
          }
          filesSniffed++;
          observeBytes += byteLength(JSON.stringify(schema));
          if (observeBytes > OBSERVE_BUDGET_BYTES) budgetExceeded = true;
        } catch (e) {
          // Not null: an explicit empty schema WITH the reason. sniff() itself
          // already returns {sheets:[],error} for a workbook it cannot open,
          // so reaching this catch means the probe blew up around it (an OOM
          // on a pathological used range used to be the classic one) — which
          // is exactly the case that must not masquerade as "unchanged".
          schema = { sheets: [], error: shortMsg(e).slice(0, MAX_REASON_CHARS) };
          warnSniffFailure(e);
        }
      }

      // Data quality checks + column profiles (P1): same discipline as sniff —
      // read-only over the file already on disk, run BEFORE add_file so an
      // unchanged (cached) file is never reopened for this either. `unchanged`
      // inherits the previous observation's checks/profiles (stay undefined);
      // a lineage-less run or one that already spent its 20s budget records an
      // explicit null so the page can tell "not run" from "inherit".
      let fileChecks, fileProfiles;
      if (!unchanged && lineage && !checkBudgetExceeded) {
        const specs = checksFor(checks, f.matched_input);
        const declared = declaredFor(inputs, f.matched_input);
        if (specs.length || Object.keys(declared).length) {
          try {
            const t0c = performance.now();
            // Argument proxies are memoised per matched_input for the whole
            // run and destroyed after the file loop: a `ref_lookup` spec
            // carries up to 50k dictionary keys, and converting that list to
            // Python once per file would burn the 20 s check budget on a big
            // folder (review 2026-08-26). run_checks only reads the specs, so
            // reuse is safe.
            const memoKey = String(f.matched_input == null ? '' : f.matched_input);
            let argPy = checkArgMemo.get(memoKey);
            if (!argPy) {
              argPy = { specs: pyodide.toPy(specs), declared: pyodide.toPy(declared) };
              checkArgMemo.set(memoKey, argPy);
            }
            let res;
            let checkProxy;
            try {
              checkProxy = lineage.run_checks(path, argPy.specs, argPy.declared);
              res = checkProxy.toJs({ dict_converter: Object.fromEntries });
            } finally {
              if (checkProxy) checkProxy.destroy();
            }
            checkMs += performance.now() - t0c;
            fileChecks = res.results;
            fileProfiles = res.profiles;
            filesChecked++;
            observeBytes += byteLength(JSON.stringify({ checks: fileChecks, profiles: fileProfiles }));
            if (observeBytes > OBSERVE_BUDGET_BYTES) budgetExceeded = true;
          } catch (e) {
            fileChecks = null;
            fileProfiles = null;
            warnCheckFailure(e);
          }
          if (checkMs > CHECK_BUDGET_MS && !checkBudgetExceeded) {
            checkBudgetExceeded = true;
            self.postMessage({ type: 'warning', msg: t('Quality checks stopped: 20 s budget exceeded — remaining files unchecked.') });
          }
        }
        // else: nothing declared/scoped for this file — fileChecks/fileProfiles
        // stay undefined, same as "not applicable" rather than "checked, none".
      } else if (!unchanged) {
        fileChecks = null;
        fileProfiles = null;
        if (checkBudgetExceeded) obsBatchBudget = true;
      }
      // unchanged: fileChecks/fileProfiles stay undefined — inherit.

      // Reading order for onepage.stage(): this file is going in NOW, so a
      // stage snapshot taken inside add_file (or later, in finish()) lists it
      // among the files read since the previous stage. Aux inputs are left out
      // on purpose — they are not observed and have no node in the graph.
      if (lineage) {
        try { lineage.note_file(f.name); } catch (e) { /* older probe */ }
      }
      const kind = session.add_file(f.name, f.mtime_ms, f.size, path);
      if (kind === 'service') {
        keepUntilFinish.push(path);       // read by load_reasons() at finish()
      } else {
        pyodide.FS.unlink(path);
        if (kind === 'cached') filesCached++; else filesParsed++;
      }

      // What the generator actually opened/read in add_file, drained per file
      // so the entry can only belong to THIS path (hooks fire synchronously
      // inside add_file, never overlapping across files in this loop).
      let consumed = null;
      if (lineage) {
        let drainProxy;
        try {
          drainProxy = lineage.drain();
          const drained = drainProxy.toJs({ dict_converter: Object.fromEntries });
          consumed = Object.prototype.hasOwnProperty.call(drained, path) ? drained[path] : null;
        } catch (e) {
          consumed = null;   // stub/broken drain() — never worth a warning of its own
        } finally {
          if (drainProxy) drainProxy.destroy();
        }
      }

      pushObserve({ dir: f.dir || 0, name: f.name, kind: kind, schema: schema, consumed: consumed, checks: fileChecks, profiles: fileProfiles });

      peak = Math.max(peak, wasmBytes(pyodide));
      if (peak > WASM_WARN_BYTES) {
        self.postMessage({ type: 'warning', msg: `High memory: wasm heap ~${(peak / (1024 * 1024 * 1024)).toFixed(2)} GB — approaching the browser limit.` });
      }
    }

    // ── 7. Finish → collect outputs from the FS ────────────────────────
    for (const argPy of checkArgMemo.values()) { argPy.specs.destroy(); argPy.declared.destroy(); }
    checkArgMemo.clear();
    progress('finishing', files.length, files.length, 'Finalizing');
    const resultProxy = session.finish();
    const result = resultProxy.toJs({ dict_converter: Object.fromEntries });
    resultProxy.destroy();
    session.destroy();
    for (const p of keepUntilFinish) {
      try { pyodide.FS.unlink(p); } catch (e) { /* already gone — fine */ }
    }

    const outputs = { slots: {}, writeBack: {} };
    const transfer = [];

    function readOut(name) {
      const u8 = pyodide.FS.readFile(OUT_DIR + '/' + name);   // Uint8Array
      // slice → standalone ArrayBuffer detached from the wasm HEAP, safe to transfer
      return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
    }

    if (result.html) {
      outputs.html = readOut(result.html);
      transfer.push(outputs.html);
    }
    for (const idx in (result.slots || {})) {
      const name = result.slots[idx];
      const ab = readOut(name);
      outputs.slots[idx] = { name: name, buf: ab };
      transfer.push(ab);
    }
    for (const idx in (result.write_back || {})) {
      const name = result.write_back[idx];
      const ab = readOut(name);
      outputs.writeBack[idx] = { name: name, buf: ab };
      transfer.push(ab);
    }

    // ── 8. Persist the OPFS cache (best-effort — quota is non-fatal) ────
    if (mountInfo && typeof mountInfo.syncfs === 'function') {
      try { await mountInfo.syncfs(); }
      catch (e) { self.postMessage({ type: 'warning', msg: 'Could not persist the cache (storage quota).' }); }
    }

    // Contract: the last (partial) observe batch must reach the page BEFORE
    // 'done', so it can be persisted alongside this run's gauges.
    flushObserve(true);

    self.postMessage({
      type: 'done',
      outputs: outputs,
      gauges: {
        filesParsed: filesParsed, filesCached: filesCached, peakWasmBytes: peak, filesSkipped: unreadable,
        filesSniffed: filesSniffed, sniffMs: sniffMs, observeBytes: observeBytes,
        filesChecked: filesChecked, checkMs: checkMs, stages: stagesOut.length,
      },
    }, transfer);
  } catch (err) {
    // Same contract on the failure path: whatever lineage data was collected
    // before the error must not be lost with the run.
    flushObserve(true);
    throw err;
  }
}

// ══════════════════════════════════════════════════════════════════════
// /explore mode (Leg 3). Same worker, same Pyodide boot, different job:
// index the user's folder into DuckDB views (static/py/_explore.py) and
// answer SQL over them. Nothing here touches the run path's state, and the
// run path never calls into it.
//
// Protocol (page -> worker):
//   {type:'explore:init',   pyodideBase, catalog}
//   {type:'explore:index',  id, files:[{name, buf, table|table_id, sheet,
//                                       partition, header_row, size, mtime_ms}]}
//   {type:'explore:sql',    id, sql, limit}
//   {type:'explore:export', id, sql}
//   {type:'explore:check',  id, checks}   -- "Check folder" (H2), no LLM
//   {type:'explore:cache_probe',  id, files:[…]}
//   {type:'explore:cache_clear',  id}
//   {type:'explore:cache_export', id, keys:[…]}   -- phase 9.2: portable cache
//   {type:'explore:cache_import', id, key, buf}
//   {type:'explore:reset'}
// worker -> page: 'explore:ready' | 'progress' | 'explore:indexed' |
//                 'explore:result' | 'explore:file' | 'explore:error' |
//                 'explore:check_result' | 'explore:cache_probed' |
//                 'explore:cache_cleared' | 'explore:cache_exported' |
//                 'explore:cache_imported'
//
// The partition cache is PORTABLE (phase 9.2): 'explore:cache_export' hands
// back the Parquet bytes of the partitions this browser indexed (transferred,
// never copied) and 'explore:cache_import' takes another user's bytes in after
// the engine has validated them. `parser_version` rides on 'explore:cache_probed'
// so the page can refuse a manifest written by a different parser.
// ══════════════════════════════════════════════════════════════════════

let exploreBase = '';           // pyodideBase, remembered for the lazy openpyxl install
const EXPLORE_LIB = '/lib/_explore.py';
const FOLDER_CHECK_LIB = '/lib/_folder_check.py';
let explorePyodide = null;      // the booted runtime (one per worker, reused)
let exploreCacheMount = null;   // OPFS mount info for /cache (syncfs after writes)
let exploreMod = null;          // PyProxy of _explore
let folderCheckMod = null;      // PyProxy of _folder_check ("Check folder", H2)
let exploreCatalog = null;      // {table_id -> {name, ...}} from the snapshot
let exploreBooting = null;      // in-flight boot promise — init is idempotent
let exploreExportReady = false; // openpyxl installed (lazy: only on export)

// PyProxy -> plain JS, always destroying the proxy. Every _explore entry
// point returns a dict/list, and every one of them leaked without this.
function pyOut(proxy) {
  if (proxy === null || proxy === undefined) return proxy;
  if (typeof proxy.toJs !== 'function') return proxy;
  try {
    return proxy.toJs({ dict_converter: Object.fromEntries });
  } finally {
    try { proxy.destroy(); } catch (e) { /* already destroyed */ }
  }
}

// The catalogue snapshot indexes tables by `table_id`; _explore.py names its
// DuckDB views by `name` (the catalogue's snake_case identifier, which is
// also what the query builder and the LLM write SQL against). This is the
// only place the two are bridged.
function exploreTableName(f) {
  if (f.table) return String(f.table);
  const tv = exploreCatalog && exploreCatalog[String(f.table_id || '')];
  return tv ? String(tv.name || '') : '';
}

async function exploreBoot(pyodideBase) {
  progress('boot', 0, 1, t('Loading query engine'));
  const { loadPyodide } = await import(pyodideBase + 'pyodide.mjs');
  const pyodide = await loadPyodide({ indexURL: pyodideBase });
  // python-calamine + pandas read the sheets (the same readers the run path
  // and the lineage probe use, so /explore sees the same used range and the
  // same header offset a generator would). duckdb answers the questions.
  await pyodide.loadPackage(['python-calamine', 'pandas']);
  try {
    await pyodide.loadPackage('duckdb');
  } catch (e) {
    // Self-hosting is the normal deployment (deploy/fetch-pyodide.py mirrors
    // the wheel), so this means the deployment is missing it or the proxy ate
    // the request. Say so plainly — a silent fallback to "no results" would
    // look like the folder was empty.
    throw new Error(t('DuckDB query engine failed to load from {p1} — contact admin (duckdb package is not self-hosted). {p2}', {p1: pyodideBase, p2: shortMsg(e)}));
  }
  const res = await fetch(new URL('/static/py/_explore.py', self.location.href).href);
  if (!res.ok) throw new Error(t('Failed to load _explore.py: HTTP {p1}', {p1: res.status}));
  const src = await res.text();
  pyodide.FS.mkdirTree('/lib');
  pyodide.FS.writeFile(EXPLORE_LIB, src);
  // "Check folder" (H2): a sibling module, same load idiom as _explore.py
  // itself — it imports `_explore` as a plain module once both are on the
  // path, no bundle, no generator involved.
  const checkRes = await fetch(new URL('/static/py/_folder_check.py', self.location.href).href);
  if (!checkRes.ok) throw new Error(t('Failed to load _folder_check.py: HTTP {p1}', {p1: checkRes.status}));
  pyodide.FS.writeFile(FOLDER_CHECK_LIB, await checkRes.text());
  await pyodide.runPythonAsync(`
import os, sys
if '/lib' not in sys.path:
    sys.path.insert(0, '/lib')
os.makedirs('${INBOX}', exist_ok=True)
import _explore
import _folder_check
`);
  // Partition cache: Parquet files in OPFS (`op-explore-cache`), mounted at
  // /cache so _explore.py reads and writes them as plain files. Best effort —
  // without OPFS the index still works, it just re-parses every time.
  try {
    const opfsRoot = await navigator.storage.getDirectory();
    const dir = await opfsRoot.getDirectoryHandle('op-explore-cache', { create: true });
    pyodide.FS.mkdirTree('/cache');
    exploreCacheMount = await pyodide.mountNativeFS('/cache', dir);
  } catch (e) {
    exploreCacheMount = null;
    try { pyodide.FS.mkdirTree('/cache'); } catch (e2) { /* read-only FS */ }
  }
  explorePyodide = pyodide;
  exploreMod = pyodide.pyimport('_explore');
  folderCheckMod = pyodide.pyimport('_folder_check');
  return pyodide;
}

async function exploreSyncCache() {
  if (exploreCacheMount && typeof exploreCacheMount.syncfs === 'function') {
    try { await exploreCacheMount.syncfs(); } catch (e) { /* quota: next index re-parses */ }
  }
}

// openpyxl is a PyPI pure wheel mirrored under {base}wheels/ — not part of the
// Pyodide dist. Installed lazily, on the first export only: the overwhelming
// majority of /explore sessions never export, and micropip costs a fetch and
// an import that the query path has no use for.
async function exploreEnsureExport(pyodideBase) {
  if (exploreExportReady) return;
  await explorePyodide.loadPackage('micropip');
  const micropip = explorePyodide.pyimport('micropip');
  try {
    // openpyxl needs et_xmlfile. Both wheels are mirrored under {base}wheels/
    // by deploy/fetch-pyodide.py; when BOTH are there we install them
    // together with deps=false, so an air-gapped deployment never reaches for
    // pypi.org. If the mirror is incomplete we fall back to the plain PyPI
    // name and let micropip resolve the closure the usual way.
    const wheel = (n) => new URL(pyodideBase + 'wheels/' + n, self.location.href).href;
    const local = [wheel('et_xmlfile-2.0.0-py3-none-any.whl'),
                   wheel('openpyxl-3.1.5-py2.py3-none-any.whl')];
    let haveLocal = true;
    for (const u of local) {
      try {
        const head = await fetch(u, { method: 'HEAD' });
        if (!head.ok) haveLocal = false;
      } catch (e) { haveLocal = false; }
    }
    // `micropip.install` is an attribute read on a PyProxy, which allocates a
    // NEW PyProxy for the bound method on each access — bind it once so the
    // proxy can be destroyed explicitly, the same discipline as `micropip`
    // and `specs` right below.
    const install = micropip.install;
    try {
      if (haveLocal) {
        const specs = explorePyodide.toPy(local);
        try {
          // callKwargs: `deps` is keyword-only in practice — passing it
          // positionally would land on micropip's `keep_going`.
          await install.callKwargs(specs, { deps: false });
        } finally {
          specs.destroy();
        }
      } else {
        await install('openpyxl');
      }
    } finally {
      install.destroy();
    }
  } finally {
    micropip.destroy();
  }
  exploreExportReady = true;
}

async function exploreMessage(msg) {
  if (msg.type === 'explore:reset') {
    if (exploreMod) { try { exploreMod.reset(); } catch (e) { /* nothing to free */ } }
    self.postMessage({ type: 'explore:ready', tables: [], stats: { tables: 0, partitions: 0, rows: 0, bytes: 0 } });
    return;
  }
  if (msg.type === 'explore:init') {
    exploreBase = msg.pyodideBase || exploreBase;
    // Idempotent: the page re-inits after re-picking a folder, and booting
    // Pyodide twice in one worker would double the ~200 MB heap.
    if (!explorePyodide) {
      if (!exploreBooting) exploreBooting = exploreBoot(exploreBase);
      try {
        await exploreBooting;
      } finally {
        exploreBooting = null;
      }
    }
    exploreCatalog = {};
    const tables = (msg.catalog && msg.catalog.tables) || [];
    for (const tv of tables) {
      if (tv && tv.table_id) exploreCatalog[String(tv.table_id)] = tv;
    }
    const catPy = explorePyodide.toPy(msg.catalog || { tables: [] });
    try {
      pyOut(exploreMod.set_catalog(catPy));
    } finally {
      catPy.destroy();
    }
    self.postMessage({
      type: 'explore:ready',
      tables: pyOut(exploreMod.tables()),
      stats: pyOut(exploreMod.stats()),
    });
    return;
  }
  if (!exploreMod) throw new Error(t('Engine not yet initialised — reconnect folder.'));

  if (msg.type === 'explore:index') {
    const files = msg.files || [];
    const errors = [];
    let indexed = 0;
    let cached = 0;
    // Where the time goes, per stage, summed over this message's files —
    // the page adds its own share-read time and shows the total breakdown.
    const timings = { fs_write: 0, parse: 0, coerce: 0, register: 0, view: 0, cache_write: 0, cache_read: 0 };
    const addMs = (out) => { const m = (out && out.ms) || {}; Object.keys(m).forEach(k => { if (k in timings) timings[k] += m[k] || 0; }); };
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      progress('index', i, files.length, f.name);
      const table = exploreTableName(f);
      if (!table) {
        errors.push({ name: f.name, error: t('file not linked to catalogue table') });
        f.buf = null;
        continue;
      }
      if (f.cached && f.cache_key) {
        // Served from the Parquet cache: the page never read the workbook.
        try {
          addMs(pyOut(exploreMod.index_cached(String(f.cache_key), table,
            f.partition === null || f.partition === undefined ? '' : String(f.partition), f.name, f.sheet || '')));
          indexed++;
          cached++;
        } catch (e) {
          errors.push({ name: f.name, error: t('cache read failed: {p1}', {p1: shortMsg(e)}) });
        }
        continue;
      }
      const path = INBOX + '/' + f.name;
      const tw = Date.now();
      try {
        explorePyodide.FS.writeFile(path, new Uint8Array(f.buf));
      } catch (e) {
        errors.push({ name: f.name, error: shortMsg(e) });
        f.buf = null;
        continue;
      }
      timings.fs_write += Date.now() - tw;
      // Drop the transferred ArrayBuffer the moment it is on the FS — the
      // same O(1) discipline as the run path's add_file loop. index_file()
      // unlinks the FS copy itself, so at no point do two copies of a
      // workbook coexist beyond one call.
      f.buf = null;
      try {
        addMs(pyOut(exploreMod.index_file(path, f.sheet || '', f.header_row || 0, table,
          f.partition === null || f.partition === undefined ? '' : String(f.partition), f.name,
          f.cache_key ? String(f.cache_key) : null)));
        indexed++;
      } catch (e) {
        // One unreadable workbook must not cost the whole folder: record it,
        // keep going, and let the page show a thinned index as thinned.
        errors.push({ name: f.name, error: shortMsg(e) });
        try { explorePyodide.FS.unlink(path); } catch (e2) { /* index_file already did */ }
      }
      if (wasmBytes(explorePyodide) > WASM_WARN_BYTES) {
        self.postMessage({ type: 'warning', msg: t('Much data in memory — exclude some partitions.') });
      }
    }
    progress('index', files.length, files.length, '');
    // Semantic views (and the OPFS sync) only once per index RUN, on the
    // page's last message — rebuilding them after every file re-ran the
    // definitions' aggregates over the whole index each time (quadratic).
    let semantics = null;
    const ts = Date.now();
    if (msg.final) {
      // Build first, sync after: the build writes the materialised views'
      // Parquet into /cache too, and that has to reach OPFS with the rest.
      semantics = pyOut(exploreMod.semantics_status());
      await exploreSyncCache();
    }
    timings.semantics = Date.now() - ts;
    self.postMessage({
      type: 'explore:indexed', id: msg.id || null, indexed: indexed, cached: cached, errors: errors,
      tables: pyOut(exploreMod.tables()),
      stats: pyOut(exploreMod.stats()),
      cache: pyOut(exploreMod.cache_stats()),
      // ADR-13: which semantic tables/views/KPIs stand on what is indexed now.
      semantics: semantics,
      timings: timings,
      peakWasmBytes: wasmBytes(explorePyodide),
    });
    return;
  }

  if (msg.type === 'explore:cache_probe') {
    // Which of these partitions are already parsed: the page skips reading
    // those files entirely. Keys are computed here so page and worker can
    // never disagree on the recipe.
    const recs = msg.files || [];
    const keys = recs.map(r => exploreMod.cache_key(r.name, r.size || 0, r.mtime_ms || 0, r.sheet || '', r.header_row || 0));
    const keysPy = explorePyodide.toPy(keys);
    let present;
    try { present = pyOut(exploreMod.cache_probe(keysPy)); } finally { keysPy.destroy(); }
    self.postMessage({ type: 'explore:cache_probed', id: msg.id || null,
      keys: keys, cached: keys.map(k => !!present[k]),
      parser_version: present.parser_version || null,
      stats: pyOut(exploreMod.cache_stats()) });
    return;
  }

  if (msg.type === 'explore:cache_export') {
    // Phase 9.2: hand the Parquet bytes of already-parsed partitions to the
    // page, which uploads them so ANOTHER user's browser can import them
    // instead of re-reading the source workbooks. The buffers are
    // TRANSFERRED — a folder's worth of partitions is not worth copying.
    const keys = (msg.keys || []).map(String);
    const keysPy = explorePyodide.toPy(keys);
    let blobs;
    try { blobs = exploreMod.cache_export(keysPy); } finally { keysPy.destroy(); }
    const parts = [];
    const transfer = [];
    try {
      for (const k of keys) {
        const v = blobs.get(k);
        if (!v) continue;
        // A python `bytes` crosses as a buffer proxy over WASM memory:
        // copy it out (slice) and release BEFORE the heap can move.
        let ab = null;
        if (v.getBuffer) {
          const b = v.getBuffer();
          try { ab = b.data.slice().buffer; } finally { b.release(); }
        } else {
          ab = new Uint8Array(v.toJs ? v.toJs() : v).slice().buffer;
        }
        if (v && v.destroy) v.destroy();
        parts.push({ key: k, buf: ab });
        transfer.push(ab);
      }
    } finally {
      if (blobs && blobs.destroy) blobs.destroy();
    }
    self.postMessage({ type: 'explore:cache_exported', id: msg.id || null, parts: parts }, transfer);
    return;
  }

  if (msg.type === 'explore:cache_import') {
    // The bytes come from the server manifest, i.e. from another user — the
    // engine validates them (Parquet magic + a real DESCRIBE) before they can
    // be served as a partition. A rejected import is an honest {ok:false}.
    const bytes = new Uint8Array(msg.buf || new ArrayBuffer(0));
    const bytesPy = explorePyodide.toPy(bytes);
    let out;
    try { out = pyOut(exploreMod.cache_import(String(msg.key || ''), bytesPy)); } finally { bytesPy.destroy(); }
    if (out.ok) await exploreSyncCache();
    self.postMessage({ type: 'explore:cache_imported', id: msg.id || null,
      ok: !!out.ok, bytes: out.bytes || 0, error: out.error || null });
    return;
  }

  if (msg.type === 'explore:cache_clear') {
    const out = pyOut(exploreMod.cache_clear());
    await exploreSyncCache();
    self.postMessage({ type: 'explore:cache_cleared', id: msg.id || null, removed: out.removed });
    return;
  }

  if (msg.type === 'explore:intent') {
    // A validated intent -> SQL text. Never executes: the page puts the SQL
    // in the editor and the run is a separate click, exactly like nl2sql.
    const intentPy = explorePyodide.toPy(msg.intent || {});
    let out;
    try {
      out = pyOut(exploreMod.intent_sql(intentPy));
    } finally {
      intentPy.destroy();
    }
    out.type = 'explore:intent_result';
    out.id = msg.id || null;
    self.postMessage(out);
    return;
  }

  if (msg.type === 'explore:sql') {
    const out = pyOut(exploreMod.run_sql(String(msg.sql || ''), msg.limit || 1000));
    out.type = 'explore:result';
    out.id = msg.id || null;
    self.postMessage(out);
    return;
  }

  if (msg.type === 'explore:check') {
    // Deterministic data-quality findings over whatever is already indexed —
    // no run, no LLM. `checks` (optional) is the page's own list, already
    // shaped {type, table, column, ...} — see _folder_check.py's docstring.
    const checksPy = explorePyodide.toPy(msg.checks || []);
    let findings;
    try {
      findings = pyOut(folderCheckMod.run(checksPy));
    } finally {
      checksPy.destroy();
    }
    self.postMessage({ type: 'explore:check_result', id: msg.id || null, findings: findings });
    return;
  }

  if (msg.type === 'explore:export') {
    await exploreEnsureExport(exploreBase);
    // Python `bytes` crosses as a buffer PyProxy (not an implicit copy), so
    // it is converted and destroyed explicitly; the `.toJs` guard keeps this
    // working if a future Pyodide starts handing over a TypedArray directly.
    const bytes = exploreMod.export_xlsx(String(msg.sql || ''));
    let ab;
    try {
      const u8 = (typeof bytes.toJs === 'function') ? bytes.toJs() : bytes;
      ab = u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
    } finally {
      if (bytes && typeof bytes.destroy === 'function') {
        try { bytes.destroy(); } catch (e) { /* already gone */ }
      }
    }
    self.postMessage({ type: 'explore:file', id: msg.id || null,
      name: msg.name || 'explore.xlsx', buf: ab }, [ab]);
    return;
  }
}

// Node test hook only (tests/checks_filter.js) — `self`/Worker globals don't
// exist under node, so this never runs in the browser/Worker context.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { checksFor, declaredFor, takeStages, takeStagesBudgeted, observePayload, byteLength };
}
