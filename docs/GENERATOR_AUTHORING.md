# How to write a report generator for OnePage

A guide for generator developers. The canonical working example is the
`slreport` pilot (Service Level): look at its code alongside this document in
the history — it has passed every gate described below. The full research
base and the rationale behind the decisions: `docs/RFC_GENERATORS_RESEARCH.md`.

**The platform's law, and it applies to your code too: every nanosecond
counts.** OnePage serves a warm dashboard in single-digit microseconds — a
generator has no right to squander that with heavy HTML, bloated data, or
minutes of computation where seconds would do. The rule is simple: better to
spend a week on the generator than to make hundreds of views pay for
carelessness. Everything marked "mandatory" below is checked at acceptance.

---

## 1. Architecture: what a generator is

A generator is a **trusted Python package** that the platform executes **on
the user's machine** (a browser engine on Pyodide/WebAssembly). The source
Excel files never leave the user's machine: only the results go up — a
finished HTML dashboard and data files.

Run lifecycle:

1. The user picks a data folder (File System Access API).
2. The engine checks the limits (see §2) BEFORE the start — exceeding a limit
   is a diagnosis, not a crash halfway through.
3. Pyodide boots in a Worker, dependencies are installed, your bundle is
   unpacked, and a `begin(...)` session is opened.
4. Files are streamed into the session **one at a time** (`add_file`), O(1)
   memory.
5. `finish()` assembles the outputs; the platform publishes them atomically
   (publish first, then a best-effort write of the write-back files into the
   folder — a file open in Excel cannot wipe out a finished computation).

The contract is the same for the browser engine and the future local agent —
write your code as if it knew nothing about the browser (no calls to JS or
the environment).

## 2. Hard limits of the browser engine

Checked by the server at registration and by the engine before every run.

| Limit | Value | Source |
|---|---|---|
| Files in folders (total) | ≲ 500 (recommendation) | platform owner's decision; the cost is folder enumeration at ~63 ms/file over SMB |
| Total size | ≲ 500 MB (recommendation) | measured engine ceiling |
| Single file | ≲ 35 MB (recommendation) | measured parsing ceiling |
| Tab memory | wall at ~2.75 GB; the engine warns from 2.4 GiB | measured on the pilot |
| Device | ≥ 2 GB RAM, ≥ 2 cores | engine gate |
| Bundle zip | ≤ 20 MB | platform config |
| Published html/slot | ≤ 50 MB each | publish limit |
| Aux file (input) | ≤ 10 MB | upload limit |

The first three rows are **recommendations, not hard ceilings** (owner's
decision of 2026-08-20). `data_scale` in the manifest may either narrow them
for your report or declare more — registration no longer rejects that. A
folder beyond these numbers does not block the run: the engine shows a
warning with the actual numbers and a «Run anyway» button, and the analyst
decides. Beyond these numbers the engine may hit the tab's memory wall
(~2.75 GB) — that is exactly what the warning is about. The remaining rows of
the table (memory, device, zip and publish sizes) stay hard.

**Text tables (`.csv` / `.tsv`).** The engine lists them on a par with Excel
workbooks (`.xlsx/.xlsm/.xls/.xlsb`); everything else in the folder is skipped
as "not an Excel/CSV file". The file is streamed into the Pyodide FS in 8 MB
chunks (the worker's memory never holds a whole copy, just one copy in MEMFS),
after which `add_file` receives it as an ordinary `path`. The lineage probe
reads the header and runs `checks` through the same code as for a workbook:
one "sheet" named `csv` (or `tsv`), the delimiter guessed from the first 64 KB
(`; , TAB |`), UTF-8 encoding with an optional BOM. The generator must read
the CSV itself (stdlib `csv` in streaming fashion, not `pandas.read_csv` in
one go: 300 MB in pandas inside Pyodide means gigabytes). A speed reference:
333 MB / 3.9 million rows through `csv.reader` with aggregation into a dict —
~25 s in WASM. Write `inputs[].match` for such an input without the extension
(`sales_price*`), so that both xlsx and csv satisfy `required: true`.

Engine start on a corporate laptop (2 cores / 4 GB) costs **9.8 s cold /
7.0 s warm** — that is a fixed price per run, do not inflate it with
unnecessary dependencies.

## 3. Bundle layout

A zip archive with `manifest.json` at its **root** and your package next to
it:

```
slreport-bundle-2.1.0.zip
├── manifest.json
└── slreport/
    ├── __init__.py
    ├── engine.py        ← entrypoint.module
    ├── dataload.py
    └── frontend/…       ← templates/JS for your HTML, if needed
```

No `__pycache__`, `.git`, or hidden files. Versions on the platform are
**immutable**: every upload is a new version, the `is_current` pointer moves,
and old bytes are never overwritten. The bundle's SHA-256 becomes your
session's `schema_version` — a code change automatically invalidates the
aggregate cache for all users (§6).

## 4. manifest.json — full reference

Unknown top-level keys are a **registration error** (on purpose: the
manifest is a contract, and a typo must be visible). The pilot's example:

```json
{
  "name": "slreport",
  "version": "2.2.0",
  "entrypoint": { "module": "slreport.engine" },
  "params": [],
  "deps": [
    { "name": "pandas",          "version": "3.0.3" },
    { "name": "python-calamine", "version": "0.8.2" },
    { "name": "openpyxl",        "version": "3.1.5" },
    { "name": "xlrd",            "version": "2.0.2" }
  ],
  "inputs": [
    { "match": "Stock_*.xlsx", "role": "data", "required": true,
      "sheets": [
        { "name": "Orders", "columns": ["SKU", "Qty", "Date"] }
      ] },
    { "match": "SL_NWD.xls", "role": "reference" }
  ],
  "outputs": {
    "html": true,
    "slots": [
      { "index": 0, "name": "sl_bc_data.bin.gz", "type": "data" },
      { "index": 1, "name": "sl_unassigned_reasons.xlsx",
        "type": "xlsx", "write_back": true }
    ]
  },
  "aux_inputs": [
    { "name": "sl_assigned_reasons.xlsx",
      "label": "Run with reasons",
      "download_from": "sl_unassigned_reasons.xlsx" }
  ],
  "required_files": ["SL_NWD.xls"],
  "data_scale": { "max_files": 500,
                  "max_total_bytes": 524288000,
                  "max_file_bytes": 36700160 },
  "engine": "auto",
  "write_back": true
}
```

| Field | Rules |
|---|---|
| `name` | `^[a-z0-9][a-z0-9_-]{0,63}$`; a new version's bundle must carry the same name |
| `version` | your own author label (1–64 characters); the platform keeps the version number itself |
| `entrypoint.module` | dotted path of the Python module with the `begin` function |
| `params` | reserved (types `string/number/boolean/choice/date`); NOT passed to the run yet — do not rely on them |
| `deps` | exact pins `{name, version}`; the server decides the fate of each one (see §8) |
| `inputs` | up to 32 entries; declared inputs are the file patterns and sheets/columns the generator reads. `match`: glob (`**/.*/?` without `/` and `..`), case-insensitive; `role`: `data` or `reference` (optional, defaults to `data`); `source`: `"folder"` (default, a file from the folder) or `"platform"` (a platform reference table — requires `table`); `table`: the reference table's name on the platform (mandatory with `source: "platform"`); `required`: bool; `sheets`: an array of `{name, columns}` where `name` is a glob over sheets or `*`, and `columns` is an array of columns (an object with `name` and optionally `type: text|number|date` — required for platform inputs). When `inputs` is non-empty, the engine reads exactly the matching files and skips the rest (legacy behaviour when `inputs` is absent). A generator with a platform input must declare ALL its other inputs in full (no hidden globs). |
| `outputs.html` | `true` — the generator returns a finished dashboard; `false` — only the slots are updated |
| `outputs.slots` | indexes are unique, ≥ 0; `write_back: true` — the file is additionally written back into the user's data folder |
| `write_back` | top-level flag: the engine needs readwrite access to the folder |
| `aux_inputs` | up to 3 entries; `name` — the strict name of the input file (`^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$`, no `..`), `label` — the caption of the second run button (1–64), `download_from` — which write-back output is downloaded under this name (must exist and have `write_back: true`) |
| `required_files` | up to 8 strict names (the same mask, no `..`, no overlap with `aux_inputs`); sugar for `inputs: {match: name, role: reference, required: true}` with case-insensitive matching on disk; the engine checks they are present before the start — a missing supporting workbook becomes a diagnosis card rather than a silent fallback inside the generator |
| `subfolders` | integer 0..4 (default 0 — only the files of the folder itself). The browser engine walks that many levels of subfolders; a file from subfolder `8/11.08.xlsx` is seen by the generator as **`8__11.08.xlsx`** — a flat name, unique across subfolders, and the same name goes into lineage, the cache and the report index. `inputs` patterns and exclusion rules are matched against both the full name and the base name (`*.xlsx` keeps working). The agent engine does not walk subfolders. |
| `engine` | `auto` / `browser` / `agent`; `browser` with an unusable dependency → 422 |
| `data_scale` | the recommendations of §2; can be narrowed or widened — registration does not reject |

### What the person pressing Run sees of all this

The `inputs` masks are not only a contract for the server; they are text the
analyst reads. The run panel has a **«What we will read…»** button: it lists
the files of all connected folders with size and date and, next to each,
states which of your masks it matched. Two consequences for the author
follow:

* **A file that matches no mask is silently not read by the engine** — the
  screen now names such files out loud, and below the list it says which
  declared masks found no file at all (a required one gets a separate line:
  the run will not start on it). Write `match` so that it makes clear what
  the file has to be called: a person will fix `OPS_RDC_Health_*.xlsx`
  themselves, but not `data*.xlsx`.
* **An extra file is removed by the analyst, not the author.** An unchecked
  box is an exclusion mask in `input_overrides` on the (generator, folder)
  pair; it survives subsequent runs and shows up in lineage as `excluded`.
  You do not need — and should not add — an author key in the manifest for
  this: the manifest is frozen with the version, while "this file in this
  folder is extra" is an operational decision that gets revisited without
  releasing code.

* **A renamed file is no longer a dead end.** If the export was named
  differently from what the mask declares, the analyst says «read as
  `OPS_*.xlsx`» on the same screen — the file enters the run under that input
  and satisfies it, including a required one. This is NAMING a file with a
  literal name, not widening the mask: the semantics of `inputs` do not
  change, and `globmatch` remains the single answer to the question "what was
  read". The decision lives on the same (generator, folder) pair, survives
  runs and shows up in lineage as an ordinary `matched_input`; the manifest
  author does not need to do anything for it.

A caveat worth knowing: on the "run on this computer" path aliases do not
work yet — there the file list is compiled by the agent, which does not know
about them yet. In the browser engine they work.

### Row-level security (ADR-18): the `rls` block

The optional `rls` block in the manifest declares which column of each slot
file restricts its rows to the viewer's territory/position. The block itself
is only a DECLARATION, frozen with the version; filtering happens on the
server each time a slot file is served (`GET /render/{id}/files/{slot}`), not
in the generator's code. The owner/admin enables the filter for a dashboard
as a separate step (`PUT /api/dashboards/{id}/rls`) — registering a version
with an `rls` block does not by itself switch anything on for existing
reports; it only writes the policy into `dashboard.rls_json` after
publishing.

Example — the Delta pilot (`example of pages/DWH/delta-bundle/manifest.json`):

```json
"rls": {
  "slots": [
    { "index": 0, "column": "tmr_position_name" },
    { "index": 1, "column": "tmr_territory_name" },
    { "index": 2, "key": "customer_number",
      "via": { "slot": 1, "key": "customer_number", "column": "tmr_territory_name" } },
    { "index": 3, "column": "tmr_territory_name" },
    { "index": 4, "column": "tmr_territory_name" },
    { "index": 5, "column": "tmr_position_name" },
    { "index": 6, "column": "territory" }
  ],
  "passthrough": [7]
}
```

| Field | Rules |
|---|---|
| `slots[].index` | a slot index from `outputs.slots`; every `outputs.slots` entry must be covered — either here or in `passthrough` (otherwise registration is rejected: an uncovered slot means a 403 for every non-`see_all` viewer in production) |
| `slots[].column` | the name of a column IN THE SLOT FILE ITSELF (not in the source SQL export — if the names differ, `column` names what actually sits in the file); a slot row is visible to the viewer if the column's normalized value is among their keys |
| `slots[].via` | indirect scope — when a slot has no territory column of its own (e.g. a per-customer breakdown): `{slot, key, column}` means "take from slot `via.slot` the set of `via.key` values of the rows where `via.column` matches the viewer's keys, and show in THIS slot only the rows whose `slots[].key` is in that set". The source slot (`via.slot`) must itself be declared with `column` — a nested `via` (via on via) is forbidden |
| `passthrough` | indexes of slots without data (JS libraries and the like) — served unfiltered to anyone who has access to the dashboard at all |

**Requirements for an RLS report** (mandatory for registration, part of the
point of ADR-18):

- the report must be **interactive** (`dash_type = "interactive"`, data in
  slot files) — a snapshot dashboard with a `bootstrap-snapshot` baked into
  the HTML does not qualify by definition: a snapshot looks the same to
  everyone, there is nothing to filter, and an attempt to enable RLS on a
  static report is rejected (409);
- **all data lives in slots**; nothing sensitive may be baked into the
  report's HTML (in variables, in inline JSON) — the server filters only slot
  files, and the HTML is served as is to everyone who opens the dashboard;
- **every slot from `outputs.slots` must be covered** by the policy — either
  `column`/`via`, or an explicit `passthrough`; registration with an
  uncovered slot is rejected by the same check that cross-checks `rls`
  against `outputs.slots`;
- **dictionaries are not stable across viewers.** For dictionary-encoded
  formats (OPC1) the server rebuilds a column's dictionary from the rows that
  survived the filter — which means the dashboard MUST NOT assume a fixed
  order or the complete list of dictionary values: code that said "I take the
  list of all territories from the file descriptor" will break for a filtered
  viewer (they will see only their own). If the report needs a complete list
  of territories for the UI (not for access rights), that is a separate
  Reference Table (ADR-16), not a side effect of the data descriptor.

**Importing access scopes** (administrator, `/admin/scopes`) accepts CSV in
two formats:

- **wide** — a header with a `tmr_position_name` column (like the real
  `v_positions` export): one row = one TMR position with its HOU→RM→CM→TMR
  hierarchy and the person's `{level}_userprincipalname`/`{level}_login` at
  each level; the importer builds the tree edges itself and resolves the
  UPN/login to a `person_id`;
- **long** — the manual format (the same one the test suite uses): columns
  `person`/`role`/`node`/`parent` (header synonyms are accepted, casefold),
  one row = one "person — hierarchy node" pair with an optional parent; the
  `TMD` role with no node means `see_all`.

Both formats support `dry_run` (writes nothing, only counts) and fully
replace previously imported rows (`source='import'`) on a re-run — manual
grants (`source='manual'`, the «grant see_all» form) are left untouched by
either of them.

## 5. Engine contract (must be followed exactly)

```python
# entrypoint module
def begin(out_dir: str, cache_dir: str, schema_version: str, progress) -> "Session":
    """Open a computation session. Called once per run."""

class Session:
    def add_file(self, name: str, mtime_ms: int, size: int, path: str) -> str:
        """The user's file is already at `path`. Return exactly one of:
        "parsed"  — the file was parsed now (aggregates updated);
        "cached"  — the file was recognised from the cache, parsing skipped;
        "service" — a supporting file (reference table, override). In the
                    BROWSER the engine keeps its copy until finish() is done;
                    all other copies are deleted IMMEDIATELY after return. In a
                    NATIVE run `path` is the analyst's real file and nobody
                    touches it (see "Native run" below). In both cases the
                    path must not be kept after return."""

    def finish(self) -> dict:
        """Write the outputs into out_dir and return their names (relative to out_dir):
        {"html": "dashboard.html" | None,
         "slots": {"0": "sl_bc_data.bin.gz", ...},
         "write_back": {"1": "sl_unassigned_reasons.xlsx"} | {}}"""
```

Semantics whose violation gets the bundle rejected at acceptance:

- **O(1) in files.** After `add_file` only aggregates remain of the file.
  Collecting DataFrames into a list and concatenating them in `finish()` is
  forbidden: the pilot's naive version on a year of data hit the tab ceiling
  (980 MB and growing), the disciplined one holds **a flat 468 MB**.
- **`progress(stage, done, total, msg)`** — every call crosses the
  Worker→page boundary (postMessage). Call it once per file and once per
  major phase, never inside a loop over rows.
- **No I/O** outside `out_dir`, `cache_dir` and the given `path`s. No
  network, no threads, no subprocesses — wasm has none of them, and the code
  must stay correct in native CPython too (that is how it is tested, §10).
- **Determinism.** Same inputs → **byte-identical** outputs. Sort everything
  that comes from OS sets/dicts; fix the column order; do not bake the
  generation time into the data. This is not aesthetics: the equivalence
  gate and an honest cache both rest on determinism.
- `aux_inputs` files arrive through the same `add_file` (classify them as
  `"service"` by name) before the data files.
- **Folders are invisible to the generator.** The analyst can connect up to
  8 folders (the main one + 7 extra, e.g. a network share with a reference
  workbook); the engine passes a flat union to `add_file` — classify files
  strictly **by name**. The engine rejects the same name in two folders
  before the start, so there is never any ambiguity; declare the supporting
  workbooks without which the computation is wrong in `required_files` —
  their absence stops the run with a diagnosis, not with silent degradation.

### Native run on the analyst's machine (2026-10-19)

The same contract is now executed not only in the browser. The agent
(`agent/`, also known as the desktop mirror) can run your bundle **natively,
under ordinary CPython**, reading Excel/CSV directly from the folders on the
analyst's disk declared in `folders.ini`. The server stays in the cloud and
stays the conductor: the page, under the person's session, opens the run,
pushes the bundle to the agent on `127.0.0.1`, listens to progress and
publishes the result — for you this changes nothing about WHAT `finish()`
returns, but it changes four things about where the input files live and what
you may do with them.

**There is one contract — R2, the one described above.** It is executed
natively by `agent/r2host.py`: a separate process that imports your module,
calls `begin()`, feeds `add_file()` and returns `finish()`, repeating the
browser worker's semantics literally; it talks line-delimited JSON with the
agent, and the driver is `agent/generator.py::run_r2_subprocess`. The former
console `python -m <module> --input-dir … --output-dir …` is declared
**legacy**: it is documented nowhere, not checked by the manifest,
implemented only by slreport, and kept solely for the GreenPlum path, where
the files are put into the inbox by `gpsource`. It is not promised to new
generators, and the host does not fall back to it: a module without
`begin()` gets a `gen_no_r2` refusal, not a silent run some other way. For
the sake of honesty: the header of `agent/generator.py` used to say "one
convention for the browser and the agent", and that was untrue — the agent
ran the legacy CLI, the browser ran R2. The gap between documentation and
code cost a separate investigation, so it is recorded here rather than
erased.

**1. `path` is the analyst's real file, not a copy in a sandbox.** In the
browser the engine puts a copy of the file into the wasm FS, so the `service`
disposition there means "do not delete this copy until `finish()`", while
`parsed`/`cached` are deleted right away — a memory saving over a copy nobody
minds losing. Natively there is no copy: `path` points to the workbook in the
person's folder (often in OneDrive). The agent deletes NOTHING, renames
nothing and keeps nothing open; the disposition becomes purely informational
— it goes into the run log and the observations but affects nothing. For you
this is a new contract rule:

- the file is **read-only**; `os.remove`, `os.rename`, appending, temporary
  files next to it — forbidden;
- **do not keep it open after `add_file`**: open, read, close within the
  call. A lazily read `pd.ExcelFile(path)` stored in the session "for later",
  or an unclosed descriptor, holds a lock, and on Windows the person cannot
  save their workbook while the run is in progress;
- **do not keep `path`** after return — this rule existed before; natively
  the engine simply stops catching it (there is nothing to delete), which
  means you have to catch it.

```python
# NOT ALLOWED: keeps the workbook open until finish() — Windows won't let it be saved
class Session:
    def add_file(self, name, mtime_ms, size, path):
        self._books[name] = pd.ExcelFile(path)   # lazy reading "later"
        return "parsed"

# NOT ALLOWED: this is the analyst's working file, not a copy in a sandbox
        os.remove(path)
        os.rename(path, path + ".done")

# ALLOWED: open, read, close — and forget the path
    def add_file(self, name, mtime_ms, size, path):
        rows = CalamineWorkbook.from_path(path).get_sheet_by_index(0).to_python()
        self._accumulate(name, rows)
        return "parsed"
```

**2. Do not bake absolute paths into the report snapshot.** Measured on
slreport: the generator puts the input folder's path into `dashboard.html`.
In the browser that is a harmless `/inbox` (the name of the directory in the
wasm FS), but natively the real path on the person's disk ends up there —
`C:\Users\ivanov\OneDrive - …\SL\2026` — and everyone who opens the report
will see it, including via an anonymous share link. The rule: nothing that
`add_file` received in `path` goes into `dashboard.html`, the slots or
`write_back`; the only thing a report is entitled to know about its inputs is
the file names (`name`). The platform already has an authoritative,
permission-protected place for paths: provenance and the data sources page
(`source_files.path`, `data_sources.root_path`, `root_id`), visible only to
those who can update the report's data. The check in your §10 gates is
simple: a `grep` of the outputs for the `out_dir`/`path` substring of the
first file yields zero.

```python
# NOT ALLOWED: the path goes out to everyone who opens the report
meta = {"source": os.path.dirname(path), "generated_at": now()}

# ALLOWED: only the name — the analyst sees it in the folder anyway
meta = {"source_files": sorted(self._names)}
```

**3. The input set is not frozen.** The browser read a snapshot of the folder
through the File System Access API; the agent reads a live disk: OneDrive
finishes syncing a workbook, a person saves it mid-run. That is why the
`mtime_ms` and `size` you receive in `add_file` are re-taken by the host
**right before the call**, not taken from a folder walk a minute old —
otherwise the cache key `hash(schema|name|mtime|size)` would point to an
entry with someone else's content. If the file changed between the walk and
the call, or changed DURING your `add_file`, the run does not stop but gets a
warning (`input_changed` / `input_modified`) that reaches the page. For you
there is one consequence: treat `mtime_ms`/`size` as the truth at the moment
of the call and key the cache on them alone — do not cache by name, and do
not do a second `stat` yourself "for reliability": two snapshots diverging
within one run will diverge from the worker.

**4. The cache can get poisoned — and the host, not you, takes care of it.** A
run killed halfway (Ctrl-C, OOM, a laptop unplugged) leaves a half-written
entry in `cache_dir`; its key will match on the next run, your code will read
it as `cached` and produce **a silently wrong report** — there is nothing to
notice. The host puts a `RUNNING` marker (`.r2host-running`) into `cache_dir`
before `begin()` and removes it ONLY after a successful `finish()`; a marker
found at start means "last time we did not get to the end", the cache
directory is wiped entirely (there is nothing to repair selectively — which
entry is half-written cannot be seen from outside), and the run gets a
`cache_reset` warning. What is required of you is exactly what §6 required
anyway: the cache is best-effort, reads are verified, and no files of your
own in `cache_dir` with names starting with a dot.

**What this means for the §10 gates.** Native CPython and Pyodide are
different environments: `np.array([1]).dtype` is `int32` in wasm and `int64`
natively; `sys.maxsize` is 2 147 483 647 versus 9 223 372 036 854 775 807;
the sum of eight 2³⁰ values is 8 589 934 592 natively and 0 in wasm. pandas
holds `int64` explicitly, so its path is safe; the risk remains wherever the
generator calls bare numpy without a `dtype`. The tool
`tools/engine_parity.py` runs one bundle on one folder twice — in Pyodide
under node and natively — and compares the outputs layer by layer (bytes,
then decoding and numbers). On slreport 2.2.25 and 36 synthetic workbooks the
result is **WARN**: the slot is byte-identical, 3527 numeric literals in the
html matched, and only the timestamp and the input folder path differed
(item 2 above). This proves parity of **one generator on one data set** — and
nothing more; the gate has to be run on every bundle that will go into a
native run.

### Read observation

The engine records which sheets and columns the generator actually opened and
compares them with the declaration in the manifest (`inputs` +
`required_files`). This information goes into the lineage graph and lays the
groundwork for Data Quality (plan P3.2).

**Instrumentation.** Before the uploaded bundle imports its code, the engine
calls `_lineage.install_hooks()` (loaded from `static/py/`):
  - **python-calamine** — sniffs the sheet names and the first row (header)
    via `iter_rows()`, records the file's path in the engine before
    `add_file`, and on reading notes which sheet was opened (sheet-level,
    because calamine returns rows, not columns).
  - **pandas.read_excel** — intercepts `usecols` and the DataFrame's internal
    indexes to record the columns read (only on explicit selection).

**Blind spots (two honest limits).**
  - **Cached files:** files with the `cached` status (recognised from the
    cache by name/size/mtime and not reopened) never fall under the hooks.
    The platform shows "consumed: as of run #N" and takes the structure from
    the previous observation. Sniffing still works (the header is read by
    sniff() BEFORE `add_file`, while the file is on disk).
  - **Agent engine (GreenPlum):** a subprocess with frozen SQL code; no hooks,
    lineage is only what is declared in the manifest's `inputs`.

**Observation budget.** Three limits, each in its own place:
  - Structural JSON per observations batch (BEFORE saving to the DB): `_MAX_OBS_BODY` = 2 MB on the server (app/services/observations.py). Beyond that the batch is rejected (413).
  - Per-file schema: if a file contains more than `MAX_SCHEMA_SHEETS` sheets (server, app/services/observations.py) or a sheet has more than `MAX_SCHEMA_HEADER_CELLS` characters in its header, this is recorded as `truncated: true` at the `source_observation` level (the whole folder observation). The sniff in the worker (static/py/_lineage.py) uses its own `MAX_SHEETS`/`MAX_COLUMNS` limits and stops sniffing when the `OBSERVE_BUDGET_BYTES` budget (~2 MB of accumulated JSON) is exceeded.
  - The final `truncated` on `source_observation` is set by the server when `final=true` arrives: if the schema was truncated because of size, it is marked (independently of the worker flag, which is not persisted).

### Quality checks (checks)

Data quality is a triple: declared vs observed (schema_*), freshness, and
**checks on content** that you define in the manifest. Checks run **in the
browser** on the analyst's machine before publishing; the results are sent to
the server and take part in the Data Quality findings feed.

**Manifest contract.**

The `checks` field is an array (up to 64 entries). Each check:

```json
{
  "match": "Stock_*.xlsx",  // Mandatory — an exact match of one of inputs[].match (not a glob)
  "sheet": "Data",          // Optional, defaults to "*" (any sheet)
  "column": "SKU",          // Mandatory — a column name declared in inputs for this match/sheet
  "type": "enum",           // One of: not_null, unique, range, regex, enum, known_values
  "values": ["A", "B", "C"], // enum only: an array of up to 500 strings of 64 characters
  "min": 0,                 // range only: numbers (min and/or max, both present)
  "max": 100,
  "pattern": "^[A-Z]+$",    // regex only: a string of up to 200 characters, a compiled regex
  "on_fail": "warn",        // Optional, defaults to "warn" (reserved: "block" not yet supported)
  "label": "Warehouse Code" // Optional, the caption in the user interface (64 characters)
}
```

**Validation (happens at registration):**

1. `match` must match one of `inputs[].match` **exactly** (no glob).
2. `column` must be declared in `inputs[matched_input].sheets[*].columns[]`
   for a sheet that can overlap with `sheet` (both are globs).
3. `sheet` and `column` are trimmed when applied (`.strip()`), but the id key
   is built from the original text: renaming a column makes a new check.
4. Type parameters (`values`, `min`/`max`, `pattern`) are checked for format:
   - `enum.values`: a list, non-empty, up to 500 elements, each a string ≤ 64 characters
   - `range.min`/`max`: numbers; `min ≤ max` if both are present
   - `regex.pattern`: a string ≤ 200 characters, compiles as a Python regex
5. Duplicates are an error. The duplicate key is `(match, sheet, column,
   type)`: two checks of DIFFERENT types on the same column are not a
   duplicate (e.g. `not_null` and `known_values` on one column are allowed
   at the same time, see the manifest example below); only the same `type` on
   the same file/sheet/column is a duplicate.

**How checks are executed.**

The browser engine calls `run_checks()` from `static/py/_lineage.py`
**before** `add_file` for every file that matches one of `inputs[].match`.
Checks run over the file's content via `python-calamine`, one sheet at a time:

1. **Header:** the first 20 rows are scanned, looking for a row that meets
   three conditions at once: (a) it contains ≥2 non-empty cells and they span
   ≥50% of the table width; (b) ≥60% of the non-empty cells are strings; (c)
   all strings in that set are unique (case-insensitive). The scan STOPS at
   the first row that passed condition (a), even if it failed (b) or (c) — the
   first full-width row is the table, whatever it looks like. If no such row
   is found, row 0 is used. The number of the header found is stored in
   `header_row`.
2. **Data scan:** up to 200,000 rows per **sheet** (set by `MAX_CHECK_ROWS`,
   `static/py/_lineage.py`). If the sheet is larger, the results are marked
   `sampled: true`.
3. **Time budget:** checks run **sequentially across files** (not in
   parallel). The total time of all checks across all files in one run must
   not exceed 20 seconds (the engine budget, `CHECK_BUDGET_MS = 20000`).
4. **In-memory limits:**
   - `known_values`: only up to 500 distinct values (then `distinct_overflow: true`)
   - `unique`: only up to 2000 distinct values (cardinality threshold)
   - All values that leave the analyst's browser are cut to 64 characters

**Results (what is sent to the server).**

For each check `run_checks()` returns:

```json
{
  "check_id": "enum:Stock_*.xlsx:Data:SKU",  // type:match:sheet:column (stable id)
  "sheet": "Data",
  "column": "SKU",
  "status": "pass" | "fail" | "skipped",
  "count": 247,                      // Non-empty cells scanned
  "failed": 3,                       // Cells that failed the check
  "examples": ["X001", "Y002", "Z003"], // ≤5 examples, each ≤64 characters
  "distinct": ["A", "B", "C"],       // For known_values: ≤500 distinct values
  "distinct_overflow": false,        // For known_values: there were more than 500 values
  "sampled": false,                  // true if the sheet contained more than 200,000 rows
  "reason": null                     // For skipped: "sheet_missing", "column_missing", "unreadable", "cardinality_exceeded", "bad_pattern"
}
```

In addition, profiles are computed for **declared columns** (in `inputs[]`):

```json
{
  "sheet": "Data",
  "column": "Date",
  "type": "date",                    // Data type: "number", "date", "text", "mixed" or "empty"
  "nulls": 0,                        // Empty cells in the column
  "count": 247,                      // Non-empty cells scanned
  "distinct": 234,                   // Distinct values (null on overflow)
  "min": "2026-01-01", "max": "2026-12-31", // Min/max: for numbers, dates AND text
                                      // (for text — the shortest/longest cell);
                                      // inside the engine they are computed separately per type
                                      // (nmin/nmax, dmin/dmax, tmin/tmax), but in the
                                      // payload they are always returned as "min"/"max"
  "top": [["2026-06-15", 3], ["2026-07-20", 2], ...], // ≤5 (value, count), each ≤64 characters
  "sampled": false                   // true if the sheet contained more than 200,000 rows
                                      // OR the number of distinct values
                                      // exceeded max_distinct (distinct overflow) —
                                      // not only "the sheet is large"
}
```

**Server-side semantics.**

The server receives exactly what the browser passed through `run_checks()`:
examples, profiles, value types — **for the first time, table cells leave the
analyst's browser**. No other data is sent. The server:

1. **First run:** check results are stored in the `quality_results` table.
   The `known_values` signal (the list of distinct values) **becomes the
   baseline set** on the server — this is what the server considers "known".
   On all subsequent runs the `known_values` check looks for values that are
   NEW (not in the baseline).
2. **Subsequent runs:** the platform compares the current results with the
   baseline and surfaces the new values found.
3. **Acceptance (`POST /api/data-sources/{source_id}/baselines/{generator_id}`):**
   the analyst can explicitly accept the new values, extending the baseline.

**Cached files and updates.**

- **A cached file** (recognised by mtime/size and not reopened by the
  generator): checks are **not run** — the file is considered unchanged and
  its results are inherited from the previous run.
- **A changed file:** when the generator reopens it, the check runs **before**
  `add_file` (the file is still on disk). The new check's results replace the
  old ones.

**Honest limits.**

- **A legacy generator** (no `inputs` in the manifest): checks do not run at
  all. Such a generator gets only freshness and row trends.
- **A sheet without a header.** When `sniff()` finds no sheet header (an
  empty sheet, a header beyond 20 rows, a read error — see
  `header_row`/`reason` above), Data Quality reports it as a separate finding
  kind, `schema_unreadable` ("Header not found in «file» / sheet: reason"):
  a warning for an input with the `data` role, informational for the rest.
  The exception is a reason caused by the engine's own observation budget (the
  schema was not read / was truncated to fit the batch limit) rather than by
  the file: such a reason is always informational and comes with the hint
  "raise the limit / fewer sheets", not as a data warning.
- **Agent engine (GreenPlum):** checks are not supported. No data is provided
  for quality analysis, only the declared inputs.
- **History is limited to the observation window.** The findings history is
  returned by `app.services.quality.evaluate()` based on the dashboard's last
  10 runs (the `:HISTORY_RUNS` window on the server). The check results
  themselves (`quality_results`) are subject to a retention policy
  (`app.services.quality.prune`, run once per calendar day): for each source
  at least `max(30, HISTORY_RUNS × number of dashboards reading the source)`
  most recent observations are kept in full; for older ones, within 180 days,
  one observation per ISO week remains (weekly resolution); everything older
  than 180 days (and the other observations within an already-kept week)
  loses its `source_files` and with them the `quality_results` that depend on
  them. The observations themselves (`source_observations`), value baselines,
  acknowledgements (acks), annotations and `run_stages` are not subject to
  this policy and are not deleted.

**Manifest example with checks:**

```json
{
  "name": "slreport",
  "version": "2.3.0",
  "entrypoint": { "module": "slreport.engine" },
  "inputs": [
    {
      "match": "Stock_*.xlsx",
      "role": "data",
      "required": true,
      "sheets": [
        {
          "name": "Orders",
          "columns": ["SKU", "Qty", "Date", "Warehouse"]
        }
      ]
    },
    {
      "match": "SL_NWD.xls",
      "role": "reference"
    }
  ],
  "checks": [
    {
      "match": "Stock_*.xlsx",
      "sheet": "Orders",
      "column": "SKU",
      "type": "not_null",
      "label": "SKU cannot be empty"
    },
    {
      "match": "Stock_*.xlsx",
      "sheet": "Orders",
      "column": "Qty",
      "type": "range",
      "min": 0,
      "max": 1000000,
      "label": "Order quantity in valid range"
    },
    {
      "match": "Stock_*.xlsx",
      "sheet": "Orders",
      "column": "Date",
      "type": "regex",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
      "label": "Date in YYYY-MM-DD format"
    },
    {
      "match": "Stock_*.xlsx",
      "sheet": "Orders",
      "column": "Warehouse",
      "type": "enum",
      "values": ["NYC", "LA", "CHI", "MIA"],
      "label": "Known warehouse codes"
    },
    {
      "match": "Stock_*.xlsx",
      "sheet": "Orders",
      "column": "SKU",
      "type": "known_values",
      "label": "SKU catalog baseline"
    }
  ],
  "outputs": {
    "html": true,
    "slots": [
      { "index": 0, "name": "sl_bc_data.bin.gz", "type": "data" }
    ]
  },
  "deps": [
    { "name": "pandas", "version": "3.0.3" },
    { "name": "python-calamine", "version": "0.8.2" }
  ]
}
```

### Platform reference tables

A reference table is a table of mappings, uploaded to the platform once by an administrator and reused by several generators and reports. Its purpose: when codes arrive regularly and maintaining their mapping should not be your job (e.g. a code→human-readable breakdown), the reference table keeps the truth on the platform, shows unmapped values in Data Quality, and lets analysts choose: apply the reference table during a run or work with the raw codes.

**Manifest contract.**

An input entry (in `inputs[]`) that pulls a reference table from the platform:

```json
{
  "match": "<slug>.xlsx",
  "role": "reference",
  "source": "platform",
  "table": "<slug>",
  "sheets": [
    {
      "name": "data",
      "columns": [
        { "name": "nwd_bv", "type": "text" },
        { "name": "business_unit", "type": "text" },
        { "name": "name", "type": "text" }
      ]
    }
  ]
}
```

Rules for a platform input:

- `match` **must** equal `<slug>.xlsx` (the reference table arrives under the name `nwd_bv_map.xlsx` if `table: "nwd_bv_map"`).
- `source: "platform"` — a flag saying this is a platform reference table, not a local file.
- `table` — the name of the reference table on the platform.
- `sheets` must contain the data structure of the platform table — the header, the names and **types** of the columns (`text` / `number` / `date`). The first column is the mapping key.
- The schema is **frozen** when the generator is registered: types do not change. Registration checks that the reference table exists and the schema matches — if it does not match, you get a 400 with a diff and must fix either the manifest or the reference table.
- If the reference table has not been created on the platform yet, registration goes through (the reference table will be uploaded later), but nothing will work without it.

**Restriction on legacy manifests.** A generator that adds a platform input to its manifest **must declare all its other inputs in full at the same time** (if you previously relied on `required_files` or a complex glob in `inputs`, rework it into an explicit list). The reason: a platform input switches the engine from "read everything that matches" to "read exactly what is declared" (allow-list). For the SL pilot this means: manifest 2.2.20 gets real `inputs` for `SL_*.xlsx` with sheets and columns — what was previously hidden in the code.

**What the generator receives.**

A platform reference table arrives as the supporting workbook `<slug>.xlsx` in `add_file()`:

```python
def add_file(self, name, mtime_ms, size, path):
    if name == "nwd_bv_map.xlsx":
        # The reference table is present if the analyst chose «Apply reference table».
        # The order in which files arrive is NOT guaranteed (the reference table may
        # arrive after the data), and the contribution of data files is usually cached
        # by name|size|mtime — so only remember the path here, and read and apply
        # the mapping in finish(), to the aggregate already assembled.
        self._bv_map_path = path
        return "service"          # the file stays on disk until finish() is done
    ...

def finish(self):
    bv_map = {}
    if self._bv_map_path:
        df_map = pd.read_excel(self._bv_map_path, sheet_name="data")
        # Columns: nwd_bv, bv, bv_name + the service column _key_norm
        # (normalized key) — use it for lookups.
        bv_map = dict(zip(df_map["_key_norm"], zip(df_map["bv"], df_map["bv_name"])))
    ...
```

Important: give a platform input **`required: false`**. The pre-run check for
required files sees the reference table only when the «Apply» checkbox is on;
`required: true` would make the "without reference table" mode impossible
("required input file missing").

A data pattern may overlap with a name from `required_files`
(`SL_*.xlsx` × `SL_NWD.xlsx`): the exact name wins — `inputs_for` puts
`required_files` first, and a file belongs to the first matching input. The
supporting workbook stays a reference, and the glob takes all the weekly files
(`SL_03.xlsx`, `SL_01_02.xlsx`, `SL_07 (1).xlsx`). Do not narrow the glob for
the sake of supporting workbooks: `SL_??.xlsx` silently loses `SL_01_02.xlsx`
— in allow-list mode a file outside the pattern is not read and shows up only
in Data Sources as "unmatched".

The file always has a fixed format:

- Sheet `data` (always).
- Header in row 1 (always).
- Key in the first column.
- Service column `_key_norm` (last) — holds the normalized key for lookups; computed by the `onepage.norm_key()` function.

**Two modes: with the reference table and without.**

The generator must work in both modes:

1. **«Apply reference table»** — the analyst ticked the checkbox for the run, the reference table is loaded → the file `<slug>.xlsx` is present → you get the mappings.
2. **Reference table switched off** — the file is **not passed**, you work with the raw codes; your code must cope with that without `try/except` and without failures.

Both modes **must be covered by tests** at generator acceptance.

**Key normalization.**

The `onepage.norm_key(s)` function (available at runtime):

```python
import onepage

key = "NWD BV -  2026"
normalized = onepage.norm_key(key)  # → "nwd bv - 2026"

# Rule: trim, collapse whitespace (including U+00A0 NBSP and other Unicode),
# unify dashes (U+2010–U+2015, U+2212 → "-"), casefold
# Leading zeros are NOT stripped: "00123" stays "00123"
# None/NaN → ""
```

A reference table is printed as entered but compared normalized. Use `_key_norm` from the reference table or the function directly for all lookups: this guarantees that your output and the platform check agree.

**The `ref_lookup` check type.**

A new check type for platform reference tables (in `checks[]`):

```json
{
  "match": "SL_*.xlsx",
  "sheet": "*",
  "column": "BV",
  "type": "ref_lookup",
  "table": "nwd_bv_map"
}
```

Rules:

- `match` — a glob over files, as always.
- `sheet` — a glob over sheets.
- `column` — the column name (must be declared in `inputs[].sheets[].columns`).
- `type: "ref_lookup"` — a check against a platform reference table.
- `table` — the reference table's name (must match one of `inputs[].table` with `source: "platform"`).

**What the platform checks:**

The engine fetches the normalized keys from the reference table and compares them with the column's normalized values. The result: a list of **new values** — those present in the column but not in the reference table (up to 500 distinct + an overflow flag).

The check **always** runs, even if the analyst switched «Apply reference table» off — the owner needs to know about unmapped values.

On the server after publishing:

- The reference table's page shows **all unmapped values** across all reports that use this reference table.
- If you **import new keys** into the reference table, the unmapped values are closed **without recomputing the report** (the engine re-evaluates the check on read on the server).
- The report gets a "rerun needed" flag (`rerun_needed`) if the current version of the reference table is newer than the version used in the last run.

**Versioning and modes.**

- **A version** is a number. A new version is created **on every import** of the reference table — automatically.
- **The «Allow version selection» checkbox** (`allow_pin`) — whether the analyst may pick a specific version for a run, or the current one is always used.
- **Modes in the version caption:** the platform appends one of the following to the publication's `note`:
  - `· reference table <slug> v<n>` (a specific version was used)
  - `· without reference table <slug>` (the reference table was switched off)

**Reference table limits.**

- Key: ≤ 64 characters (the `MAX_CELL_CHARS` ceiling, so that there is no perpetual false unmapped value).
- Table: ≤ 20,000 rows.

| Field in the manifest table | Rules |
|---|---|
| `source` | (new field) One of: `"folder"` (default, a file from the folder), `"platform"`. A platform source = a reference table. |
| `table` | (new field, only with `source: "platform"`) The reference table's name on the platform (`^[a-z0-9][a-z0-9_-]{0,63}$`). |

**In an input's `columns[]`:**

Each column gets a new `type` field (optional with `source: "folder"`, **mandatory** with `source: "platform"`):

```json
"columns": [
  { "name": "nwd_bv", "type": "text" },
  { "name": "business_unit", "type": "text" }
]
```

Values: `"text"`, `"number"`, `"date"`.

### Stages

Checks and lineage describe the **input files**. Stages describe what happens
between them and the report: the generator names its own steps, and the
platform draws them in the lineage graph between the file nodes and the
generator node.

**API.** The engine publishes the `onepage` module before importing your code
(`_lineage.install_hooks()`), so it is available both via `import onepage`
and without an import:

```python
import pandas as pd
import onepage           # provided by the engine

def add_file(self, name, mtime_ms, size, path):
    df = pd.read_excel(path, sheet_name="Data", usecols=["SKU", "Qty"])
    self.frames.append(df)
    return "parsed"

def finish(self):
    merged = pd.concat(self.frames, ignore_index=True)
    onepage.stage("merged", merged)                       # step 1
    agg = merged.groupby("SKU", as_index=False).sum()
    onepage.stage("aggregated", agg, inputs=["merged"])   # step 2, depends on step 1
    ...
```

Signature: `onepage.stage(name, df, inputs=None)` — `name` (a string, cut to
64 characters), `df` (a **pandas DataFrame**), `inputs` (an optional list of
names of previous stages). Returns `None`. The order is the order of calls;
stages are not declared in the manifest. If `inputs` is not given, the graph
connects the stage to the previous one in order.

**What is captured from the DataFrame** (at the moment of the call): name,
sequence number, `rows` = number of rows, column names (the first 256, each up
to 64 characters), a profile of the first **64** columns (type
`number|text|date|mixed|empty`, counts of empty and filled values, number of
distinct values, min/max, top-5 values with frequency) over the first
**200,000** rows (if truncated, the profile is marked `sampled`), the list of
files passed to `add_file` since the previous `stage()` call (reading order,
not proof of dependency — the graph draws these edges dashed), and the time
taken to capture the snapshot.

**What goes to the server.** Only what is listed above: **column names,
numbers and the profile's top-5 values, cut to 64 characters**. Not a single
whole data row, no "first N rows" — as with `checks`, these are the only cells
the server sees. Stages travel in the final observations batch (`stages` next
to `files`) and are replaced wholesale on every run.

**Limits.**
  - No more than **32** stages per run; calls beyond the limit are ignored.
  - `stage()` **never raises**: a non-DataFrame, an empty name, an exotic
    dtype — the call is silently ignored. A report cannot crash because of a
    stage.
  - A snapshot costs time and memory (a pass over ≤ 64 columns × ≤ 200,000
    rows). Put `stage()` at meaningful boundaries (after assembly, after
    aggregation), not inside a loop over files. In total, 20 seconds are
    allotted to profiling all the stages of a run (as with `checks`); once the
    budget is exhausted, later calls still record
    name/order/rows/columns/inputs/read_files, but without the column profile.
  - The agent engine (GreenPlum) provides no stages — it has neither hooks nor
    pandas.
  - Outside the engine (a local run, your tests) the `onepage` module does not
    exist. If the generator also runs locally, wrap the import:

    ```python
    try:
        import onepage
    except ImportError:                     # local run outside the engine
        class onepage:                      # noqa: N801
            @staticmethod
            def stage(*a, **k):
                pass
    ```

## 6. Aggregate cache (mandatory)

`cache_dir` is an OPFS directory mounted into the Pyodide file system: it
**survives a browser reload**. On the pilot a warm run is **2.85–4.5×** faster
than a cold one — the user feels it on every recomputation, so the cache is
mandatory, not optional.

- Key: `sha256(f"{schema_version}|{name}|{mtime_ms}|{size}")`.
  `schema_version` is the checksum of YOUR code: any code change
  automatically resets the cache. Do not invent anything beyond this key.
- Cache the **finished aggregates of a file** in a compact binary form
  (numpy arrays, pickle protocol 5) — not raw rows and not half-processed
  data.
- The cache may be unavailable (quota, private mode): the code must work
  without it, losing only speed. Write to the cache best-effort, read with
  verification.

## 7. Performance discipline (the core of this document)

Measured facts our budgets are based on: xlsx parsing in wasm costs
×1.14–1.25 of native (not ×2 — optimise the algorithm rather than "waiting
for the agent"); engine start ~7–10 s; the pilot's full year — 26 weekly
files, 355,609 rows — is computed in tens of seconds cold and single-digit
seconds warm.

Mandatory rules:

1. **Read xlsx only with `python-calamine`** (a Rust parser). `openpyxl` is
   exclusively for WRITING xlsx; `xlrd` only for legacy `.xls`.
2. **Data types**: categories for repeating strings, `int32/float32` where
   the range is sufficient, no object columns in working structures. After
   aggregation, `del` the intermediate objects; do not keep references.
3. **Aggregate on the fly**: keep sums/counters/dicts, not rows. If the
   dashboard needs the detail, it goes into a slot file (§9) rather than
   living in the generator's memory until the end.
4. **Fewer dependencies — a faster run every time.** Every wheel is a
   download + import on every engine start. Before adding a package, prove to
   yourself that 30 lines of your own code will not solve the problem.
5. Heavy imports go at module top level (once), but no WORK at import time:
   `begin()` must return instantly.
6. Write output files in a streaming fashion; do not assemble giant `bytes`
   in memory.

## 8. Dependencies: what is available and how to add a new one

The server decides the fate of each dependency at registration, against the
vendored Pyodide 314.0.2 lock (354 packages) and PyPI:

| Status | What it is | What happens |
|---|---|---|
| `bundled` | the package is in the Pyodide distribution | the engine installs the **distribution's version** (the pin in the manifest is a declaration of intent) |
| `pypi-wheel` / `pypi-pure` | a pure `*-none-any.whl` wheel on PyPI | the wheel name is frozen; the engine installs it from our server |
| `unavailable` / `unknown` | no wasm build | `engine: browser` → 422 at registration; `auto` → agent only |

Already self-hosted (available without sign-off): `pandas 3.0.2`,
`numpy 2.4.3`, `python-calamine 0.6.2`, `xlrd 2.0.2`, `openpyxl 3.1.5`,
`micropip 0.11.1`.

**A new dependency is a procedure, not a line in the manifest.** The
corporate network blocks the CDN and pypi.org from the platform's origin
(measured on the first live run): the wheel has to be served from our server.
Before using a new package, tell the platform owner — they will add it to
the self-hosted subset (`deploy/fetch-pyodide.py`) and rebuild the deployment
bundle. Without this step your generator will fail at dependency installation
for everyone who has no direct internet access.

## 9. The output dashboard: format and weight budgets

A dashboard is **a single self-contained HTML file**. It lives in an iframe
sandbox without `allow-same-origin`: no cookies, no direct API requests,
localStorage is proxied by the platform (suitable only for small drafts),
downloads work. **No external URLs** — no CDN scripts, no fonts, no images
over http: the corporate network will not open them. All libraries are
inlined into the HTML.

Weight budgets (these are not wishes — they are platform thresholds):

- **HTML < 1.5 MB** (better — hundreds of KB): heavier, and the report's card
  on the grid loses its live preview. A thin HTML is also cheap in the
  server's render LRU cache — it warms up and is served in microseconds.
- **Data does NOT go into the HTML.** The HTML keeps a small inline
  `<script id="bootstrap-snapshot" type="application/json">` for an instant
  first render (top-level aggregates). The main dataset is a separate slot
  file (slot 0).
- **The slot file is a binary columnar format** (SoA: per-column arrays +
  dictionaries for strings), compressed with gzip/brotli and named
  `*.bin.gz` / `*.bin.br` — the platform serves it with the right
  `Content-Encoding`, and the browser decompresses it transparently. On the
  client — typed arrays over an ArrayBuffer, **no `JSON.parse` of
  megabytes**; filters materialise only the filtered subset. This is how the
  pilot holds 355,609 rows with no noticeable page weight.

How the dashboard receives data (works in share links too):

```html
<script>
  window.addEventListener('message', (e) => {
    const m = e.data;
    if (m && m.type === 'op_file' && m.slot === 0) {
      render(new Uint8Array(m.buf));   // zero-copy ArrayBuffer from the platform
    }
  });
  parent.postMessage({ type: 'op_ready' }, '*');
</script>
```

## 10. Testing — mandatory gates before hand-over

The §5 contract does not depend on the browser — the generator is tested in
native CPython with a thin harness (create a session, feed the files, compare
the outputs). Before handing the bundle over to the platform owner, the
following must be green:

1. **Determinism**: two runs on the same data → the sha256 of all outputs
   match byte for byte.
2. **Contract**: dispositions are correct (reference tables → `service`, data
   → `parsed`); a re-run with the same cache → all files `cached`; the warm
   run's outputs are byte-identical to the cold run's.
3. **Equivalence** (when refactoring an existing report): aggregates —
   element-wise, detail rows — as multisets, against the old version on the
   full real volume.
4. **Memory**: peak RSS of a native run on the FULL year of data as a proxy;
   the first browser run — under observation (the engine itself warns from
   2.4 GiB — a warning at acceptance equals failure).
5. **Weights**: HTML < 1.5 MB, slot files compressed, bundle ≤ 20 MB.
6. A run on the **full year**, not on a sample: the §2 limits are checked
   against the real maximum the user will see.
7. **Environment parity** (for a bundle that will go into a native run on
   the agent): `tools/engine_parity.py --bundle B.zip --dir FOLDER` on your
   folder gives PASS or WARN, and in a WARN only the clocks differ — not the
   numbers and not the paths (§5, "Native run"). Parity of someone else's
   generator does not carry over to yours.

Hand-over checklist: the manifest is valid (registration on the staging stand
without a 422) · all six gates green with numbers in the report ·
dependencies agreed (§8) · no external URLs in the HTML · write_back/aux names
fixed and identical in the manifest and the code.

## 11. Registration and the life of versions

The zip is uploaded by an administrator: `/admin/generators` → pick the
generator → «New version» (or register a new one). The server returns
manifest errors as a list in a single response — readable text, fixable in
one pass. On success the new version becomes current; reports run by users
will pick it up on the next run, and the aggregate cache invalidates itself
(a new `schema_version`). Rollback = uploading the previous code as a new
version: the bytes of old versions are immutable and always available.

## 12. `semantics.json` — what the report means (optional, but enables «Ask» in plain language)

Next to `manifest.json`, at the bundle root, you can put a `semantics.json` — a
description of the report **as data**: which canonical tables come out of the
input files, which rules apply before computation, how KPIs are computed, what
the terms mean. The platform checks the file at version registration (shape,
identifiers, read-only SQL, a dry run of every query over empty tables — a
typo in a column name gives the author a 400, not the operator an error),
freezes it with the version and hands it to the engine in the browser: the SQL
from the file runs in DuckDB over the files the user opened. The server still
does not see a single cell.

A minimal example:

```json
{
  "schema_version": 1,
  "generator": "slreport",
  "glossary": {"SL": "Share of ordered quantity delivered, Σmin(fact,order)/Σorder"},
  "rules": {"reporting_start": "2026-06-01"},
  "tables": {
    "sl_orders": {
      "description": "Retail chain orders, one row = one export row",
      "sources": [{"file": "SL_*.xlsx", "sheet": "Orders"}],
      "business_key": "order_no",
      "columns": {
        "order_no":    {"source": "Number", "type": "text", "role": "key"},
        "client_code": {"source": "Customer", "occurrence": 0, "type": "text", "role": "attr"},
        "client":      {"source": "Customer", "occurrence": 1, "type": "text", "role": "dim"},
        "ship_date":   {"source": "Ship date", "type": "datetime", "role": "time"},
        "order_qty":   {"source": "Order, pcs", "type": "number", "role": "measure"},
        "fact_qty":    {"source": "Fact", "type": "number", "role": "measure"}
      }
    }
  },
  "views": {
    "lines": "SELECT *, least(order_qty, fact_qty) AS fact_clipped FROM sl_orders WHERE ship_date >= DATE '{reporting_start}'"
  },
  "kpis": {
    "sl": {
      "name": "Service level", "target": 0.97, "aggregation": "ratio",
      "definition": "Σ min(fact, order) / Σ order; over-delivery does not compensate for under-delivery",
      "question_forms": ["what is the SL", "service level"],
      "cell_sql": "SELECT client, CAST(ship_date AS DATE) AS day, SUM(fact_clipped) AS n, SUM(order_qty) AS d FROM lines GROUP BY 1, 2"
    }
  }
}
```

Rules the platform checks:

- table/column/view/KPI names — `^[a-z][a-z0-9_]{0,63}$`; the `__` prefix is reserved;
- `type` ∈ text / number / datetime / date / bool — this is the type the engine **presents to SQL**;
  if a number is written as text in the file ("1 250,5"), declare `text` and cast it in a view;
- the same header appearing twice in a sheet → each column gets its own `occurrence` (0, 1, …);
- a KPI's `unit` is the display unit ("days", "mln st"): for ratio metrics it
  is MANDATORY if the metric is not a share (duration = stock/sales is days;
  without a unit the page will show percentages);
- `cell_sql` — a single `SELECT`/`WITH`, no DDL/ATTACH/COPY/read_*; returns dimensions plus
  `n` and `d` (share = Σn/Σd) or `value` (a sum); `{key}` placeholders are filled from `rules`;
- `views` are created in the declared order and see the tables and the preceding views;
- file ≤ 512 KB; tables ≤ 64, columns ≤ 256 per table, KPIs ≤ 128;
- the `description` of columns (`tables.*.columns`, `views.*.derived_columns`)
  is not help text for people but part of the prompt: the model sees each KPI
  dimension as `- rdc: <first 90 characters of the description>` and uses
  them to translate the user's words into names (`"branch" → rdc`,
  `"warehouse" → shipping_display`). Write descriptions in the user's words
  and with the dashboard's captions, without values from the data. The
  glossary goes into the prompt too, but the overall ceiling is 24 terms
  together with the platform glossary, and the bundle's terms come last: keep
  yours ≤ 20 and merge duplicates.

### `semantics.json` schema version and KPI owner

- `version` (an optional integer, top level, default `1`) — the version of
  the SCHEMA of the `semantics.json` file itself (not to be confused with
  `schema_version`, which is fixed and always equals `1`, nor with the
  generator version). A file without `version` is read as `version: 1`. The
  compatibility rule: the platform supports versions `1..N` (the current `N`
  = `semantics.SUPPORTED_VERSION`); a `version` greater than this platform
  build understands is a 400 at registration ("unsupported semantics version
  …"), not a silent best-guess reading of the file. Raise `version` only when
  you change the shape of the file itself in a way that is incompatible with
  an older platform build.
- `kpis.<key>.owner` (an optional string, ≤120 characters) — who is
  responsible for the metric's definition. It is free text: a role or a job
  title ("financial controller", "SL report owner"), NOT necessarily a
  personal name.
- `kpis.<key>.approved_at` (an optional ISO date, `YYYY-MM-DD`) — when the
  definition was agreed.

Both fields are pure annotation: the platform does not use them in
computations or SQL, but it keeps them in the generator version's frozen
`semantics_json` and shows them in the `/explore` metrics panel as a caption
under the KPI name when set.

```json
"kpis": {
  "sl": {
    "name": "Service level", "target": 0.97, "aggregation": "ratio",
    "owner": "SL report owner", "approved_at": "2026-06-01",
    "definition": "…", "cell_sql": "…"
  }
}
```

### Slot tables (ADR-14b): report data without a source folder

An agent report (data from a DB, not from an Excel folder) can give the chat a
table as a slot file. In `semantics.json` the source is declared like this:

```json
"tables": {
  "van_stock_daily": {
    "description": "day × warehouse × SKU",
    "sources": [{"slot": "van_stock_daily.csv.gz"}],
    "columns": { "...": "as usual, source = the file's column header" }
  }
}
```

Rules:
- `slot` — a bare file name, AS PUBLISHED by the generator (`slot_N`,
  original name); paths and `..` are a registration error; `slot` is
  incompatible with `file`/`object` in the same source;
- formats: `csv`/`tsv`/`txt` (and their `.gz`), `xlsx` (optional `sheet`,
  `header_row`), `json` (an array of objects or `{columns, rows}`);
- a column without `source` means "the header in the file is already
  canonical" (the usual case for a DB export); `source` is needed only if the
  header in the file differs from the semantics column name;
- deliver values AS TEXT (like a `COPY` from the DB): typing is done by the
  engine according to the declared `type` (`_sem_cast`), and it survives a
  change of conversion version;
- conversion happens on the server on every run publish — best-effort: a
  failure does not break the publish, the response carries `lake.errors`;
- such a report is always answered by the SERVER (ADR-14b); there is no
  "connect a folder" mode for slot tables;
- in the manifest's `outputs.slots` the slot is declared as usual (index +
  name); the `table`/`format` keys are allowed there, but the conversion is
  driven by the semantics, not the manifest. Do NOT add a slot table to the
  template's `agentDataParts` — `expectedSlots` is computed from its length.

### A platform reference table as a semantics table (ADR-16)

A semantics table can be taken not from the user's files but from a
**platform reference table** («Reference tables», `/reference/{slug}`). Then
an edit made by the responsible person in the web interface reaches the next
chat answer — without registering a new generator version:

```json
"tables": {
  "client_map": {
    "description": "Customer → how to display it and its segment",
    "sources": [{"reference": "sl_client_map"}],
    "business_key": "client",
    "columns": {
      "client":         {"type": "text", "role": "key", "description": "Customer code as in the export"},
      "client_display": {"type": "text", "role": "dim", "description": "Display name"},
      "client_segment": {"type": "text", "role": "dim", "description": "Customer segment"}
    }
  }
}
```

Rules:

- `reference` — the reference table's slug (`^[a-z0-9][a-z0-9_-]{0,63}$`);
  incompatible with `file`/`file_pattern`/`object`/`slot`/`bundle_config` in
  the same source, and such a table has EXACTLY one source;
- the semantics column names are the reference table's column names (case
  does not matter); `source` on a column is forbidden, inline `rows` are
  forbidden — the platform provides the rows;
- `business_key` is mandatory and must equal the reference table's key
  column;
- `type` ∈ `text` / `number` / `date` (reference tables have no
  `datetime`/`bool`);
- you may declare a SUBSET of the reference table's columns;
- **schema reconciliation at registration**: if the reference table does not
  exist yet, the version is registered and the reference table is CREATED
  empty from this declaration (see below); if it already exists, the type of
  each existing column and the key must match, otherwise a 400 with an exact
  diff; a column the reference table does not have is ADDED (additively,
  after the version has been written);
- if the SAME slug is declared both in the manifest (`source: "platform"`)
  and in the semantics, the reference table gets the UNION of the columns:
  the manifest sets the order (the server writes the run's workbook in that
  order), the semantics appends its own. The same column with DIFFERENT types
  in the two halves of the bundle is a 400 with a diff: it is a contradiction
  within one bundle, and the author fixes it;
- the row ceiling is 20,000 (`reference.MAX_ROWS`, also
  `semantics.MAX_REFERENCE_ROWS`), plus an overall 4 MB budget
  (`semantics.MAX_REFERENCE_BYTES`) for all reference tables in one response:
  rows travel as lists, not objects, so the largest reference table costs
  ≈1.5 MB. Whatever does not fit is returned as `unbound` with an honest
  status, and the rest of the report answers;
- access to the rows = the right to read the reference table itself, and that
  right follows ONLY the manifest consumers (the run actually materialises the
  workbook) plus the responsible person/creator/admin. Declaring someone
  else's slug in your own semantics is NOT a way to gain access: for a reader
  without rights the table arrives `unbound` ("no access to reference table
  …"), and "in chat" remains just a badge in the list of consumers. No
  reference table → `unbound` ("reference table … has not been created"); in
  both cases the rest of the report answers;
- a report whose tables are ALL reference tables (or whose folder has not
  been indexed yet) still reaches the chat: a bound reference table is reason
  enough to hand out the definitions. KPIs that need file tables stay "not
  ready" by their usual logic;
- **rows never reach the model's prompt** — only the column names and
  `description`, as for any other table;
- the version the chat answered from is visible in the page status:
  "reference table sl_client_map v7".

### The reference table is created automatically at version registration

Both `inputs[]` with `source: "platform"` and `sources: [{"reference": …}]` in
the semantics now CREATE the reference table if it does not exist yet: columns
and types from the declaration, the key is the first column (for semantics —
`business_key`), title = slug, `current_version = 0`, no responsible person,
`allow_pin` off, plus a row in Data Sources (`kind: table`) and an audit entry
(`reference_create`, `auto: true`). Then a person opens `/reference/{slug}`,
downloads the empty template and uploads an Excel file.

What does NOT happen (ADR-16, addendum of 2026-08-27):

- re-registering a version **never** empties or recreates an existing
  reference table: rows, versions, the responsible person, `allow_pin` — all
  intact;
- if NEW columns appear for the reference table in a new manifest, they are
  added to the schema additively; existing rows are not rewritten — missing
  values read as empty (`reference.fit_rows`), and export immediately returns
  the new column. The key and the types of existing columns are frozen;
  adding columns is allowed;
- removing a column from the declaration is not a change: the generator has
  simply stopped reading it, and the reference table keeps it. Changing a
  column's type or the key is a 400 with a diff;
- if the generator stops referring to the reference table, the reference
  table lives on — it simply stops showing this consumer. Reference tables are
  deleted only by people, via `/reference/{slug}` → «Delete» (with
  confirmation; the right is the same as for writing);
- an empty reference table produces ONE entry in Data Quality, "reference
  table is empty — upload Excel", not "100% unmapped": nothing at all maps
  into an empty table, and the counter would be noise.

On the «Reference tables» page such a reference table appears as "created from
the manifest of <generator> · 0 rows — upload Excel" with a «Download
template» button, and the list has a report/generator filter
(`/reference?generator=slreport` — the «Reference tables» link sits in the
generator's row on `/admin/generators`).

Check the file before uploading: `python -c "from app.services import semantics; import json;
print(semantics.validate_full(json.load(open('semantics.json')), 'generator-name'))"` —
an empty list means "valid". The reproducibility of the report's numbers is
checked by a separate author tool (`tools/semantics_gate.py`, see ADR-13).

Every KPI must compute in ≤ 2 s on a real folder
(`tools/real_smoke.py … --max-kpi-ms 2000`) — a slow KPI is a problem of the
report layer, not a reason for the platform to raise the worker timeout. Most
often the culprit is a `view` with several `UNION ALL`s over the same table
(it scans the source N times): replace it with a single pass using
`CASE`/`GROUPING SETS` that computes all the slices in one scan.

This rule is built into both pre-hand-over gates, not only into
`real_smoke.py`: `tools/real_smoke.py --max-kpi-ms 2000` times each KPI through
the same `intent_sql` → `run_sql` the page uses, and exits with code 1 if even
one KPI exceeded the budget (`kpi_errors` grows, `sys.exit(main())`).
`tools/semantics_gate.py` (reproducibility, ADR-13) measures the same thing in
its own pass (`kpi_offenders`, the default budget is also 2000 ms, its own
`--max-kpi-ms` flag) and likewise exits with code 1 when it is exceeded — the
gate checks both the numbers (S1 ≥ 0.8 matches against the reference) and the
speed, in one run before version registration.
