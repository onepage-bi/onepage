# RFC research: report generators launched from OnePage

Status: **v2 — research brief** (not an implementation spec; touches no code).
Date: 2026-07-20. Brief: vlad + Claude (3 rounds of Q&A, answers recorded in §3).
Related material: `docs/PERF_DECISIONS.md` (ADR culture, benchmarking rules),
`sl-report` (pilot generator + the baseline delivery `sl_launcher.exe`),
`CLAUDE.md` §Performance envelope (lessons from benchmark campaigns C0–C5).

---

## 1. The problem in one paragraph

The business wants to run report generators (today — Python packages like `sl-report`)
from the OnePage web interface: source files are read on the user's machine
(including network folders), and **only the result** goes up (HTML + slot files,
megabytes). Raw data (up to 1–10 GB) is never uploaded to the server — the principle
"the fastest byte is the one that never travelled over the network" was established by
benchmark campaigns and is not up for revision.

## 2. Baseline and gap analysis

The baseline **exists and works**: `sl_launcher.exe` + a portable Python
(double-click → code is pulled from Git → the report is built at native speed →
the result is uploaded to OnePage by hand).

Baseline pain points (what the new solution must close):

| # | Pain | Closed by |
|---|------|-----------------|
| G1 | Publishing the result to OnePage is manual | auto-publish through the browser session |
| G2 | No run status/history in the platform | viewer Data dock + run history |
| G3 | The `python\` folder is updated by hand | the platform engine delivers the runtime itself |
| G4 | "Too complicated": setup, console, errors | UX diagnosis in Phase 0 (U1), human-readable errors in the UI |

**Failure criterion for the whole project:** the new solution is worse than the baseline on
even one of these properties — native compute speed, working with network folders, writing
back into the data folder (`sl_unassigned_reasons.xlsx`), auto-updating the code with a single `git push`.

## 3. Fixed constraints (Q&A 2026-07-20)

- **C1. Browsers:** Chromium only (Edge/Chrome) — the File System Access API is available.
- **C2. Scale:** a family of scripts, SL is the pilot. The cost of adding a new
  generator is criterion #2 when choosing the architecture (porting every script to JS
  does not pay off).
- **C3. Volumes and load profiles:** 1–10 GB is real.
  - **P1** (pilot, SL year): ~50 xlsx × 2–4 MB ≈ 100–200 MB.
  - **P2**: many xlsx + CSV/text exports, 1–10 GB in total.
  - The xlsx files contain **hidden sheets that are not needed** — they must not be parsed at all.
- **C4. A hybrid is acceptable:** a localhost agent is not forbidden (formal confirmation — IT-3).
- **C5. Publishing:** at first only manually from the browser (a live `op_session` cookie;
  the agent/engine needs no OnePage secrets); scheduled runs come later;
  the RFC must not close the door to API tokens.
- **C6. Trust in code:** generator authors are only the platform owner's team,
  but the platform enforces the boundary technically (R7), not organisationally.

## 4. Brief decisions taken (v2)

- **R1. Browser-first pilot with a gate.** Engine #1 is the browser one (Pyodide), only for
  profile P1. The decision is confirmed or reversed by the Phase 0 numbers (§6, go/no-go).
  This is **pilot scope, not architecture**: P2 is not addressed by the browser engine
  (WASM32 ceiling, K4). *Status 2026-07-20: the gate on the full-year P1 was formally not passed;
  by the owner's decision the pilot continues browser-first on a narrowed P1 ("the current
  folder") with a mandatory incremental cache and registered review
  triggers — Appendix B.*
- **R2. Engine-agnostic contract.** Launch, progress, cancellation, result delivery —
  one interface for two engines: Pyodide in the browser and a localhost agent (an evolution of
  the launcher). The agent is a second engine behind the same contract, not a rewrite.
- **R3. A generator artifact = bundle + manifest**, not "a single .py": the pilot `sl-report` is
  a package (`slreport/` + `frontend/` + `requirements.txt`); a single file does not work
  even for it. Manifest: dependencies (validated against the Pyodide profile — pure
  Python, self-hosted wheels), launch parameters, result declaration (HTML, slots),
  a data-scale hint (the engine boundary).
- **R4. Code source — two registration methods converging on one artifact.**
  - **(a) Manual bundle upload** — the simplest case, works from day one.
  - **(b) Git link + key** — as in `launcher.ini` today: the API form of the zipball URL
    (`https://api.github.com/repos/…/zipball/<branch>`) + a fine-grained read-only token
    (Contents: Read-only, one repository). The platform fetches the zip **itself**, server-side.
    Both methods produce the same entity — an **immutable generator version**;
    the browser engine always receives the bundle from OnePage, regardless of the registration
    method. In Git mode the manifest lives at the repository root (next to
    `requirements.txt`).
  Critical requirements of Git mode:
  - **The token becomes a server secret** — and that is an improvement over the launcher, where
    it sits in plain text in an ini file on every machine: one storage point,
    a write-only field (once saved, the API never returns it), rotation in one place,
    usage auditing.
  - **A branch is a moving target; a version is not.** Every fetch resolves to a commit SHA
    and is snapshotted as an immutable generator version; a run references the snapshot.
    "Updated from Git" = a new version appeared in the audit log, old ones remain reproducible.
    Update semantics — parity with the launcher: check before running; no network →
    the last snapshot (the cache = the analogue of `%LOCALAPPDATA%`).
  - **The trust boundary extends to write access to the repository**: whoever can push
    to the branch executes code on analysts' machines (R7). Registering a Git source is
    admin-only, and the repository must be the team's own.
  - **The platform's first external runtime dependency.** Today OnePage does not
    reach outside at all at runtime. Server access to `api.github.com` through a proxy with
    TLS inspection is a separate question, IT-5; a network failure must not break runs
    (the cache is mandatory); Git being unavailable degrades to "build the last version",
    not an error.
- **R5. A draft state instead of an "empty dashboard".** Until the first successful run
  the dashboard is visible only to owner/admin, with a CTA placeholder "Run generation"; it does not
  exist for viewers or in lists. Only someone who can fix the emptiness ever sees it.
- **R6. Atomic publishing + races.** The result is published as one bundle:
  upload everything → new version → atomic switch of `current` (the platform's versioned storage
  already does this). No three sequential PUTs: a tab closed in the middle of the
  upload must not leave the report out of sync. A run-lock on the server
  ("Y is already running, started N min ago"). Retention: keep the last N generated versions.
- **R7. Code trust boundary.** Generator code executes first-party on the machine of whoever
  pressed "Refresh", with readwrite access to the granted folder and their session — unlike
  dashboards (a sandbox without same-origin). Therefore: uploading/replacing code is
  a separate right (admin-only), code versions are immutable, everything goes to `audit_log`,
  and before a run the analyst sees "generator vN, uploaded by whom/when".
- **R8. UX.** The "Refresh from folder" button lives in the existing viewer Data dock
  (where the slot-0 mechanics already live). The tab is a thin launcher page modelled on
  `/themes`: a list of refreshable reports + "last run: date/status" → a click
  opens the viewer with `?dock=data`. A separate run console comes after schedules.
  Errors are human-readable, in the UI, with an action ("no access to the folder → choose again"),
  not a console.

## 5. Performance model

**The multiplication law: nanoseconds matter only where they are multiplied by a large N.**

| Term | N | Unit cost | Order of contribution | Controllability |
|---|---|---|---|---|
| Reading the SMB share | 10⁸–10¹⁰ bytes | ~10–100 ns/byte effective | seconds–minutes | not ours (network); measure, don't "optimise" |
| Parsing xlsx | 10⁷–10⁸ cells | **100 ns–1 µs/cell** | seconds–minutes | **the main CPU lever: choice of parser** |
| Pyodide bootstrap | 1 (per session) | seconds cold / ~0 warm | seconds | self-hosting + browser cache |
| Orchestration/UI | 1 per run | ms | ms | irrelevant; do not optimise |
| Publishing the result | ~5–10 MB | intranet | < a second | atomic bundle (R6) |
| Viewer serve path | — | already µs (fast lane) | — | untouched by the feature |

Example of scale: 200 MB of xlsx ≈ 10⁷–10⁸ cells. 100 ns/cell → 1–10 s; 1 µs/cell →
10–100 s. In the parser's inner loop nanoseconds decide everything; in orchestration (N=1) — nothing.

**The main algorithmic lever is not to parse.** An incremental cache of parsed
weekly files (content key = mtime+size, stored in OPFS; the same trick as the platform's
render fingerprint): a weekly refresh parses **one** new file.
The second lever is lazy ZIP reading: inflate only the XML of the Orders sheet; hidden
sheets (C3) are never unpacked at all.

**Candidate budgets** (approved after S1/S2; until measured these are hypotheses, not promises):

| Metric | Budget | Note |
|---|---|---|
| Weekly incremental (1 new file, warm cache) | ≤ 15 s click→published | the main user scenario |
| Full-year cold (P1, office laptop) | ≤ 3 min | the pilot gate |
| Main thread | no long task > 50 ms | all compute in a Worker |
| Progress liveness | update ≤ 500 ms | TTFR: first file in progress < 1 s |
| Publishing (~5 MB) | ≤ 5 s | atomic bundle |
| P2 (1–10 GB) | outside the browser engine | boundary set by the manifest; refine numerically |

**Instrumentation:** `op:gen:*` performance.measure → the existing `perf.js` beacon →
`/api/perf/client`; the publish endpoint — stages in perflog. Every performance claim
in the final RFC comes with a number and its provenance (run_id discipline as in `PERF_DECISIONS.md`).

## 6. Phase 0 — pre-registered measurements

Validity rules for all measurements: real files from the share; the analyst's office laptop
(not the dev Mac, IT-4); 3 runs, median; cold/warm recorded separately; CPU measurements —
on a local copy of the files (I/O is isolated in S2).

- **U1. UX diagnosis of the launcher.** Interview 1–2 analysts: the concrete steps where
  they get stuck ("too complicated" is a symptom; we need the causes). The result may change
  the engine priority: if the pain is setup and the console, not "a native program",
  the agent is rehabilitated.
- **S1. xlsx parsing** (MB/s, cells/s, peak memory) on 3 real weekly files
  (small/typical/large): (a) native python-calamine — the launcher baseline;
  (b) native openpyxl read_only; (c) Pyodide + openpyxl read_only;
  (d) SheetJS in a Worker; (e) a python-calamine wheel for Pyodide — if one exists
  (Rust; most likely not — record the fact).
- **S2. SMB from the analyst's machine:** cold-read throughput of the full-year folder,
  per-file latency (K7: with hundreds of small files latency matters more than bandwidth).
- **S3. Pyodide bootstrap** behind the corporate proxy, self-hosted from `static/`:
  cold/warm, download size.
- **S4. Browser gates:** FSA not disabled by Edge policy (IT-1); persistent
  permissions for FSA ("Allow on every visit", Chromium ≥122 — check in your
  Edge); `pyodide.mountNativeFS` over a network-drive handle — does it work and
  what does reading cost (K6); the tab's memory ceiling before it crashes (synthetic alloc test).

**Checks IT-1…IT-5** (clarification 2026-07-20: there is no dedicated IT authority —
the items are closed by **self-checks** per `onepage-phase0/drafts/IT_selfcheck.md`;
the platform owner makes and dates the risk decisions):
IT-1 — Edge policies on the File System Access API (`edge://policy` + the fact from
gates_test.html); IT-2 — CDN reachability (expectation: no, precedent Google Fonts →
self-host Pyodide+wheels); IT-3 — localhost-listener precedent (empirical check;
a native-code precedent already exists — the launcher); IT-4 — analysts' machine profile
(msinfo32 + gates_test.html); IT-5 — outbound access from the OnePage server to
`api.github.com` through the proxy (curl probe) — needed for Git mode R4(b); without it
the pilot lives on bundle mode.

**Pilot go/no-go gate (pre-registered):** extrapolating S1+S2 to P1 gives
cold ≤ 3 min **and** incremental ≤ 15 s **and** S4 without blockers → browser-first
confirmed. Otherwise the agent becomes engine #1 and the browser stays the UI/publishing layer.

## 7. Phase 1 — topology matrix

Candidates (dominated ones are included deliberately — so the elimination is honest):

- **A.** Browser only, Pyodide (Python as is).
- **B.** Browser only, a JS port of the logic — expected to be dominated on C2 (a fork
  of business logic, two sources of truth).
- **C.** Hybrid: a localhost agent (evolution of the launcher) + the browser as conductor.
- **D.** Launcher only + auto-publish via API with no browser UI (requires tokens — C5).
- **E.** Server-side run (data travels to the server) — expected to be dominated: network,
  server CPU, an execution sandbox.
- **F.** A+C behind the common contract R2 — **the working winning hypothesis; the research
  must try to refute it, not confirm it.**

Criteria with weights (descending): (1) correctness + E2E on P1/P2;
(2) cost of adding a generator (person-days); (3) security/operations;
(4) peak RAM on users' machines; (5) UX friction (installation, prompts, updates).
The number of memory copies is an instrumental Phase 2 metric, **not** a criterion for choosing
the topology. Method: first eliminate the dominated options, then a weighted trade-off;
every number comes from Phase 0 or a new measurement; theoretical estimates are marked as
such and take no part in the decision.

## 8. Phase 2 — micro-design of the winner's pipeline

The only level where microseconds/copies are appropriate (N is large here):

- Browser engine: all compute in a Worker (main thread — UI only); transferable
  ArrayBuffer (zero-copy postMessage semantics); streaming file read and parsing as
  data arrives; incremental OPFS cache; lazy ZIP entries (K8);
  incremental per-file aggregation — never concatenate a year in memory (K9).
- Agent (when we get there): the page↔127.0.0.1 protocol (fetch-stream vs WebSocket vs
  SSE — choose by measurement; PNA preflight, K2), progress, cancellation, pairing.
- Result format: the existing `packing.py` contract (`.bin.br` slot, typed
  arrays without JSON.parse) — **do not change**, it is already optimal and battle-tested.

## 9. Phase 3 — contents of the final RFC

Generator manifest schema; code-source registration in the two R4 modes (bundle /
Git link+key: token storage, snapshot by SHA, cache, degradation without network);
API (draft state R5, publish bundle R6, run-lock,
run history); permission model R7; invalidation (the existing fingerprint+SWR
covers it — publishing a version rotates the key as usual); fault tolerance (a break at
any step does not corrupt the published version); security (the R7 boundary, manifest
validation); profiling (`op:gen:*`); load-test plan; acceptance criteria
**against the §2 baseline**; a designed (not implemented) API-token interface
for future schedules (C5).

## 10. Traps (check explicitly)

- **K1.** FSA does not reveal the absolute path of the chosen folder → "the browser chose it,
  the agent read it by path" is impossible; in the hybrid, folder selection lives with the agent.
- **K2.** Private Network Access: check the enforcement status for an HTTPS page →
  http://127.0.0.1 in current Chromium (it has changed between versions).
- **K3.** Edge group policies can disable FSA entirely (IT-1).
- **K4.** WASM32 ≈ 4 GB of address space (less in practice); do not count on Memory64
  in Pyodide.
- **K5.** The CDN is unreachable from the corporate network (precedent: Google Fonts) → self-host
  Pyodide + wheels in `static/`.
- **K6.** `mountNativeFS`: write buffering (flush/syncfs needed), the cost of reading
  through a handle — measure it (S4), don't take the documentation at its word.
- **K7.** SMB: with hundreds of small files, file-open latency matters more than bandwidth.
- **K8.** Do not inflate hidden xlsx sheets (skip the ZIP entries entirely).
- **K9.** `dtype=str` in pandas inflates memory ~5–10× the file size —
  incremental aggregation only.
- **K10.** A proxy with TLS inspection breaks external downloads (launcher precedent) —
  self-hosting (K5) removes it.
- **K11.** File junk in real folders (measured in S4): `__MACOSX/`,
  AppleDouble stubs `._*.xlsx` (hundreds of bytes, valid extension, invalid
  content), plus the classic Excel lock files `~$*.xlsx`. The engine must
  filter by pattern, and an unparseable file is skip+report, not an aborted run.

## 11. Open doors (designed, not implemented)

API tokens for schedules (C5); a run console (R8);
multi-dashboard generators (the pilot is strictly 1 generator : 1 dashboard).
Git mode R4(b) has moved from "doors" into the RFC scope — but whether it is in the pilot
depends on the IT-5 answer (without outbound server access only bundle mode remains).

---

## Appendix A. Methodological S1 run (2026-07-20, dev Mac)

**Status of the numbers:** these are NOT the pre-registered gate numbers (§6 requires the office
laptop — S1b). The run was taken on a dev Mac (Apple Silicon, macOS, local SSD) and
gives (a) a working harness, (b) **stack ratios**, which are transferable,
(c) three facts that invert the original assumptions. Node 24 (V8) is a documented
approximation of Chromium. Provenance: `spike/kit/results_native.json`,
`spike/kit/results_node.json` (method: 1 warmup + 3 runs, median; Pyodide parse
measurements taken inside the Python VM via `perf_counter`; each native engine in a separate
process for a clean maxrss).

**File:** a real weekly `SL_23.xlsx`, 2 037 597 bytes; the Orders sheet
14 294 rows × 28 columns ≈ 400 260 cells; 2 sheets in the workbook, none hidden.

| Stack | median | ns/cell | vs native calamine |
|---|---|---|---|
| native pandas 3.0.3 + **calamine** 0.8.2 (= launcher) | **0.173 s** | ~432 ns | 1.0× |
| SheetJS 0.18.5, sheet only (Node) | 0.276 s | ~690 ns | 1.6× |
| native openpyxl 3.1.5 raw (read_only) | 0.463 s | ~1.16 µs | 2.7× |
| native pandas + openpyxl | 0.616 s | ~1.54 µs | 3.6× |
| **Pyodide 314.0.2 + calamine 0.6.2 (wasm32)** | **0.397 s** | ~0.99 µs | 2.3× |
| Pyodide + openpyxl | 1.369 s | ~3.42 µs | 7.9× |

One-off Pyodide costs (Node, local network): boot 0.72 s; pandas ~0.28 s
(warm cache; cold measured at ~0.81 s); openpyxl from PyPI 0.31 s; calamine 0.22 s.

**Three facts that invert the assumptions:**
1. **python-calamine DOES have a wasm32 wheel in the lock of current Pyodide**
   (`python_calamine-0.6.2-…-pyemscripten_2026_0_wasm32.whl`) — "only slow openpyxl in the
   browser" is refuted. Pyodide+calamine is only 2.3× off native
   calamine, and 3.4× faster than Pyodide+openpyxl. Bonus: `_read_excel` in slreport already
   prefers calamine with an openpyxl fallback — the same code will pick the best engine
   in both environments.
2. **openpyxl is NOT in the Pyodide distribution** (only via micropip from PyPI) —
   self-hosting (K5) must also include wheels from PyPI, not only Pyodide CDN packages.
   Pin Pyodide by version (measured 314.0.2, ABI 2026_0, py3.14).
3. **Lever K8 (hidden sheets) depends on the size of the "extra" sheets:** the SheetJS
   `sheets` option really does limit materialisation (verified structurally),
   but on THIS file the gain is 7.8 ms — the second sheet is tiny, and the zip container
   is inflated in full. The lever remains for workbooks with heavy hidden sheets;
   on SL_23 it is not confirmed.

**Extrapolation to P1** (50 weekly files, single-threaded, parsing only — no SMB
and no publishing): native calamine ≈ 8.7 s (launcher baseline); SheetJS ≈ 14 s;
Pyodide+calamine ≈ 20 s (+~2 s one-off costs); Pyodide+openpyxl ≈ 68 s.

**Preliminary gate estimate** (confirmed only by S1b/S2): with an office-laptop
derating of ×2–4, Pyodide+calamine gives ~45–90 s cold — it passes the
≤ 3 min threshold with room to spare; Pyodide+openpyxl at ×4 (~4.7 min) does not. Conclusion:
the viability of the browser engine rests on calamine-wasm — its wheel
is mandatory in the self-host. For the incremental scenario (≤ 15 s) parsing a single file
(~0.4–1.6 s) is not the limit — the budget is eaten by the Pyodide boot and publishing (to be measured by
S3 + the prototype).

**Other:** the npm package `xlsx` 0.18.5 carries unpatched advisories (prototype
pollution, ReDoS) — a minus for the SheetJS paths in the matrix on criterion #3. Memory: the
Pyodide process after the runs has rss ≈ 887 MB (the wasm heap is outside the JS heap) — acceptable for P1;
the K4 ceiling for P2 stands. SMB terms are absent from this run (local
SSD) — they will be captured by `gates_test.html`'s S4-FSA section on the `H:` folder (which is also S2).

---

## Appendix B. Phase 0 fact log

- **2026-07-20 — IT-1 CLOSED: the File System Access API is NOT restricted by policy.**
  Provenance: an `edge://policy` export from the work laptop
  (`onepage-phase0/results/policies-edge-worklaptop-20260720.json`).
  Environment: Edge **149.0.4022.98** (auto-updating — the updater ran 2 h earlier),
  Windows 10 22H2 (build 19045), domain corp.example.com. Only two policies are set —
  `RestoreOnStartup(4)` + a SharePoint start URL; **no `FileSystem*`,
  `URLBlocklist` or JS/WASM restrictions at all** → default behaviour
  ("ask the user"). Edge 149 ≥ 122 → persistent permissions
  for FSA are available ("Allow on every visit"). One of the two blockers of the browser
  pilot is removed. Caveat: the export covers browser policies only — network
  blocks (IT-2) and AppLocker-class restrictions (for the agent) do not show up in it;
  IT-2 is checked separately (two URLs from `IT_selfcheck.md`).

- **2026-07-20 — IT-2 CLOSED, expectation refuted:** from the work laptop
  both `https://cdn.jsdelivr.net/pyodide/v314.0.2/full/pyodide.js` and
  `https://pypi.org/simple/openpyxl/` are reachable (opened in Edge). The second blocker of the browser
  pilot is removed. Self-hosting Pyodide+wheels is downgraded from "mandatory" to
  "recommended" (version pinning, independence from external uptime and
  TLS inspection) — but it is no longer a precondition of the pilot.

- **2026-07-20 — IT-3 CLOSED:** `python -m http.server 8765 --bind 127.0.0.1`
  on the work laptop started and opened from Edge with no firewall prompts —
  the localhost-listener precedent for engine #2 exists.

- **2026-07-20 — IT-4 (preliminary, from gates_test.html): the target machine
  is weak — hardwareConcurrency = 2, deviceMemory = 4** (Win10 22H2, Edge 149).
  Consequences: (a) the ×2–4 derating from Appendix A may prove optimistic —
  S1b is mandatory before go/no-go; (b) parallelising parsing across Workers is nearly pointless
  (2 threads); (c) memory budgets must be computed against the lower bound. `msinfo32` will refine
  CPU/disk, but the order of magnitude is clear.

- **2026-07-20 — S4 CLOSED (gates_test.html; full JSON —
  `onepage-phase0/results/`):**
  - FSA works from `file://` (isSecureContext=true); the picker opened; walking
    60 files / 6 folders / 218 MB took 507 ms.
  - Reading 44 xlsx / 135.9 MB: **69.4 MB/s**, open_ms 0.5–2.7 ms. IMPORTANT:
    the chosen folder ("anna stock", traces of an unpacked zip) is apparently
    a local disk, NOT `H:` → **S2 (SMB) stays open**; these numbers are the
    local-disk baseline.
  - Permissions: read=granted; readwrite granted after the prompt; the handle survived a
    roundtrip through IndexedDB and stayed granted → **"choose the folder once"
    is feasible** (Chromium ≥122 persistent permissions confirmed in practice).
  - **Tab memory ceiling: 2.75 GB** (12×256 MB, then RangeError) —
    P2 (1–10 GB) in the browser is now ruled out not theoretically (K4) but by measurement
    on the target machine; incremental aggregation (K9) is mandatory even for P1.

- **2026-07-20 — dataset finding: the P2 profile is real, also in the form of single giants** —
  the real working folder contains 30–35 MB workbooks (`real_forecast.xlsx`,
  `forecast_june.xlsx`); plus `__MACOSX/._*.xlsx` junk (AppleDouble stubs of
  177–1656 bytes with an .xlsx extension) → trap K11 recorded.

- **2026-07-20 — S2 CLOSED (gates_test.html on the real folder `H:\…\2026`):**
  - The actual P1 is smaller than the C3 estimate: **26 files / 48.5 MB** by mid-year
    (some weeks are merged: SL_01_02, SL_04_05); a full year ≈ 50 files /
    **~95 MB** — the lower bound of the earlier "100–200 MB" estimate.
  - **SMB read: 13.66 MB/s** — 5.1× slower than the local disk of the same
    machine (69.4 MB/s). A typical weekly file (~1.7 MB) reads in 95–180 ms,
    the largest (4.9 MB) in 377 ms; open 2.7–4.0 ms/file.
  - **Directory walk: 1.63 s for 26 files (~63 ms/file vs ~8.5 ms locally)** —
    SMB metadata latency is real (K7 confirmed in a mild form: visible,
    but not dominant at 26–50 files).
  - Net I/O term of the gate: a full read of the year from the share **≈ 5 s today, ~10 s by
    year end**; incremental (1 new file) **≈ 0.15 s** — I/O is not
    the bottleneck in any P1 scenario.
  - Permissions/memory in this run repeat the morning session (same values).

- **2026-07-20 — gate arithmetic after S2** (every term except parsing is measured
  in the target environment): cold year = SMB 5–10 s + Pyodide boot (seconds; the CDN is
  reachable, the prototype will give an exact measurement) + parsing 26–50 files. Parsing is the only
  unknown: on the dev Mac Pyodide+calamine is 0.4 s/file; derating the
  2-core machine ×3–5 → 1.2–2 s/file → **cold year ≈ 40–110 s** — within the
  3 min threshold, but with a thinner margin than it seemed before IT-4. Incremental scenario:
  0.15 s I/O + 1–2 s parsing + publishing — the 15 s threshold is achievable with a warm boot.
  **The go/no-go verdict is issued only after S1b** (bench scripts on the work
  laptop) — it will replace the derating guess with a measurement.

- **2026-07-20 — S1b CLOSED** (work laptop: portable python 3.14.3 from the
  launcher, calamine 0.6.2, pandas 3.0.3; weekly file ~420 thousand cells;
  JSON — `onepage-phase0/results/laptop-native-*-20260720.json`):
  - calamine **1.819 s** (4.33 µs/cell); openpyxl+pandas 6.751 s (16.1 µs/cell);
    openpyxl-raw 5.024 s (12.0 µs/cell).
  - **The derating relative to the dev Mac is uniform across all three stacks: ×10.0–10.4** —
    two to three times worse than the ×2–4 range assumed in Appendix A. The estimate was
    optimistic; the pre-registration was right to demand the target hardware.
  - Kit defect: `maxrss_mb=null` (the Windows branch of peak_mb did not run) — it does not
    affect the verdict; the timings are complete.

- **2026-07-20 — FORMAL GATE CALCULATION (§6 pre-registration, P1 = full year
  ≈ 50 files): the condition "cold ≤ 3 min" is NOT MET.**
  - Pyodide+calamine on the laptop ≈ 1.82 s × wasm overhead 1.8–2.3 (a range because of
    the calamine 0.6.2/0.8.2 version difference in the pair of Mac measurements) ≈ **3.3–4.2 s/file**.
  - Parsing the year: 50 × (3.3–4.2) = 165–210 s; + SMB ~10 s; + Pyodide boot on
    2 cores — NOT measured, estimated 10–20 s (wasm compilation is CPU-bound) →
    **total 185–240 s > 180 s** even at the lower end.
  - Incremental: 0.15 s SMB + 3.3–4.2 s parsing + boot + publishing — the 15 s threshold
    is in doubt because of the same unmeasured boot.
  - By the pre-registered rule: **"the agent becomes engine #1, the browser —
    UI/publishing"**.
  - Caveats that could change the verdict (registered BEFORE the decision):
    (a) boot is estimated, not measured; (b) the wasm overhead is carried over from the Mac;
    (c) TODAY's half-year folder (26 files) passes cold in ~125–145 s —
    the gate fails on the full-year profile, not on the current one. The cheapest way to
    remove (a)+(b): a micro-test in real Edge on the laptop (loadPyodide + calamine
    + parsing data.xlsx in a single page; the CDN is reachable — IT-2).
  - Note for Phase 1/2: SheetJS parsing (~2.8 s/file on the laptop by the
    ×10 derating, without the Pyodide boot) would pass the gate, but as a JS fork of the logic it
    violates C2. A hybrid "SheetJS parser in a Worker → columns into the Python logic" is
    a possible optimisation of the browser engine; recorded as an option, not a decision.

- **2026-07-20 — OWNER DECISION: a registered deviation from the
  pre-registration.** vlad's choice (after being shown the formal gate failure):
  **browser-first is kept** on a narrowed P1 — "the current folder" (26 files /
  48.5 MB; cold ≈ 125–145 s — within the threshold), not the full year. Accepted risk: cold
  for the full year on the weakest machine is 185–240 s, over the threshold. Conditions
  attached to the risk:
  1. **The incremental OPFS cache is a mandatory element of the pilot, not an option** (§5):
     a full cold run is paid once per machine and on cache invalidation; the regular
     weekly scenario parses one file (3.3–4.2 s) — the full-year profile
     stops being a recurring cost.
  2. The Pyodide boot micro-test in real Edge stays in the backlog — boot limits
     the **incremental** budget (15 s), not cold.
  3. The agent remains engine #2 behind the R2 contract. Triggers for revisiting the priority:
     measured cold/incremental in real Edge worse than the §5 budgets; or
     complaints from pilot users about duration; or a generator with
     a P2 profile becoming the top priority.

- **2026-07-20 — BOOT MICRO-TEST CLOSED (condition #2 of the deviation).**
  `kit/pyodide_boot_test.html`, work laptop (UA Chrome/149, 2 cores,
  deviceMemory 4, `file:` protocol, jsdelivr CDN). Two runs, both error-free,
  runtime 314.0.2 = the pin.
  - Cold (15:29): script 0.16 + core boot 5.46 + pandas 3.29 + calamine
    0.93 = **9.84 s to a ready stack**; median parse of SL_03.xlsx (1.67 MB,
    14 406×27 ≈ 389k cells) **1.93 s** (runs 2.04/1.92/1.93).
  - Warm (15:30, tab closed and reopened): 0.014 + 4.27 + 1.92 +
    0.79 = **6.99 s**; parse 2.10 s (2.10/1.95/2.11 — within the noise of the cold run).
    The HTTP cache removes the download; the remaining ~4.3 s of core is CPU-bound
    wasm compilation, the floor for boot on this hardware.
  - **Caveat (a) removed:** boot was estimated at 10–20 s, measured 9.8 cold /
    7.0 warm — better than the low end of the estimate.
  - **Caveat (b) removed:** the wasm overhead of parsing on the target hardware is
    **×1.14–1.25** relative to native calamine on the same laptop (4.95–5.40 µs/cell in the VM
    vs 4.33 natively; caveat: different files — SL_03 in the VM, SL_23 natively,
    same family), not the ×1.8–2.3 carried over from the Mac. For the second time already, carrying a number
    across machines turns out worse than measuring directly (cf. the derating ×2–4→×10).
  - **Gate recalculated with measured numbers.** Cold for the narrowed P1 (26 files):
    9.8 boot + ~1.6 walk + ~3.6 SMB + 26×(1.9–2.1) parsing + ~5–10
    aggregation/publishing ≈ **70–80 s** (the model gave 125–145 s). Cold for the full
    year (52): ≈ **130–145 s — on TIME the full-year gate (≤180 s) now
    passes**; the year now fails ONLY on memory (concat ~2.9 GB >
    2.75 GB, App. D), and that is removed by the `run(file_iter, …)` refactor, not by hardware.
  - **Incremental (15 s threshold):** tab still open (stack alive, boot=0) ≈
    **5–6 s**; reopened tab (warm cache) ≈ 7.0 + 5.6 ≈ **12.6–13 s —
    passes**; a fully cold machine ≈ 15.4–15.6 s — right on the edge.
    Conclusion: the D.4 programme (self-host + warm-up when the Data dock opens) remains
    mandatory, but the budget is already met at the measured floor.
  - Measurement limitations: `file:` protocol (not OnePage's https), parsing on the page's main
    thread (the engine will compute in a Worker — CPU parity is expected),
    a single file; repeat with pilot telemetry (D.7).

- **2026-07-20 — IT-5 (partially closed): the corporate proxy is NOT reachable from the
  prod server.** `curl -x http://proxy.example.com:9400 https://api.github.com/`
  from the server (prod-host) → `curl: (5) Could not resolve proxy` (the same with
  `-k`) — the server is outside the corporate network; the launcher path "GitHub via
  proxy" does not exist for it. **Open remainder:** direct access
  (`curl https://api.github.com/` WITHOUT `-x`) was not checked — the VPS may go out
  directly. Consequence for R4(b): `OP_GENERATORS_GIT_MODE` stays `false`
  (the endpoints answer 503); it is switched on only after a positive direct
  test. Bundle upload (the main registration path) is unaffected.

- **2026-07-20 — IT-5 CLOSED: GitHub is reachable from the prod server DIRECTLY.**
  `curl -k https://api.github.com/` without a proxy → a full JSON API response.
  Git mode R4(b) is viable via direct access (not through a proxy, as with the
  launcher on user machines — the server lives outside the corporate network).
  Caveat: the run used `-k` (no certificate verification); `git clone`
  verifies TLS itself — final confirmation will come from the first real
  `POST …/git/sync` after enabling `OP_GENERATORS_GIT_MODE=true`.
  The flag's default stays `false` (a deliberate operator switch,
  not a technical block).

- **2026-07-20 — IMPLEMENTATION OF STAGES 1–3 COMPLETE** (a parallel session
  freed the repository; the work was split across Opus/Sonnet/Haiku, integration
  and review by the main session). Server: models/permission/manifest validation with
  wasm32 computed against the vendored lock (354 packages), immutable
  versions, read-path run-lock TTL, atomic publish, git mode behind a flag,
  write-only token. UI: the `/generators` launcher, a section in the Data dock,
  `/admin/generators`. Engine: `gen-engine.js`/`gen-worker.js` per App. D
  (FSA+IndexedDB, the C.6 boundary before launch, per-file streaming,
  OPFS cache via mountNativeFS, `op:gen:*` via the standard perf.js beacon).
  **slreport refactor with an equivalence gate: ALL PASS** — the aggregate
  element by element, detail as a multiset, the snapshot identical, the warm cache
  byte-identical to the cold one (2.85–4.5× faster); memory: legacy 980 MB
  vs **468 MB for the engine on 20 files, with the peak nearly flat in the number of
  files** — the 2.75 GB ceiling is removed structurally, as App. D predicted.
  The launcher's legacy path is untouched (it served as the gate's oracle).
  **A seam bug between the two parallel developments was found and closed:** `finish()`
  reads the service workbooks (SL_NWD, manual assignments) from the data dir, but the worker
  deleted files after `add_file` and did not pass `.xls` through — the contract was extended
  with a return value `add_file → "service"|"cached"|"parsed"`
  (service files live until finish; this also closed the `files_cached` gauge).
  Platform regression gate: end-to-end smoke 50/50 PASS. Hot paths
  untouched (checked against the diff list). Left outside this session: a live run
  of the engine in Edge on the work laptop (Pyodide does not run
  headless); the first `git/sync` as a TLS check; self-hosting Pyodide in
  `static/pyodide/` (the `OP_GENERATORS_PYODIDE_BASE` knob is ready).

- **2026-07-20 — FIRST LIVE RUN: the CDN is blocked from the corporate
  origin; self-hosting went from recommendation to default.** Prod deployed,
  slreport registered, a run from the work laptop failed with
  `importScripts('https://cdn.jsdelivr.net/...pyodide.js') failed to load`.
  Diagnosis: the micro-test from THE SAME laptop loaded the same URL successfully, but from
  a `file:` page — the block is tied to the origin `https://onepage.example.com`
  (a reverse-proxy CSP or the corporate SWG; our code sends no CSP —
  verified). Caveat K5/K10 and recommendation D.4 played out exactly as they
  had been written down. Fix: `deploy/fetch-pyodide.py` — a subset of the distribution
  (core + the closure of slreport's dependencies from the lock, sha256 from the lock,
  20 MB) → `static/pyodide/` on every bundle build;
  `OP_GENERATORS_PYODIDE_BASE=auto` — self-host when the files are present, CDN
  as fallback; pypi wheels (openpyxl) are frozen into the version manifest by
  file name and installed by micropip from OUR OWN origin
  (`static/pyodide/wheels/`), with PyPI as fallback. No external hosts remain for the engine in the
  prod configuration at all.
  **Two owner decisions/fixes along the way:** (1) the engine's file-count boundary
  raised 30→100 (vlad's decision; the binding constraints of the boundary are
  in bytes, 500 MB / 35 MB per file, and they are untouched; the cost of the per-file
  engine scales with bytes, the walk is ~63 ms/file × 100 ≈ 6 s);
  (2) the pure-wheel detector was fixed: `py2.py3-none-any.whl` (openpyxl!)
  was being frozen as `unavailable` and the worker would have refused to run —
  caught by accepting the real bundle through the registration pipeline.
  The pilot bundle was rebuilt (`slreport-bundle-2.0.0.zip`, max_files=100) —
  after redeploying it is uploaded as "New version" (v2 becomes current).
  **Postscript (second live run):** Pyodide 314 refuses
  classic workers ("Classic web workers are not supported") — the
  runtime is ESM-only in workers; the micro-test did not catch this because it loaded
  Pyodide into the page. The engine was moved to a module worker
  (`new Worker(..., {type:'module'})` + `import('pyodide.mjs')`).
  A Node gate for the completeness of the self-host subset was introduced (the same distribution runs
  in Node) — it immediately caught the missing `pyodide.asm.mjs`; after
  adding it: boot + loading the whole package closure + imports — OK.
  **The third run** got to the end of the computation and failed on write-back:
  `sl_unassigned_reasons.xlsx` was open in Excel
  (`NoModificationAllowedError` from `createWritable`) — and it took down an already
  finished result. The order was fixed: publishing FIRST (the result on the
  platform is not hostage to a local lock), write-back is best-effort
  per file, degrading to a download button with a human-readable hint
  (the "open Excel" trap was reality on the very first day of the pilot).

- **2026-07-21 — THE REASONS CYCLE (`aux_inputs`): full template, a second run
  button, a success panel instead of auto-reload.** Trigger: the first
  SL pilot week — the recomputed `sl_unassigned_reasons.xlsx` had no
  place to be downloaded from on the platform; uploading reasons through the dashboard interface
  turned out to be a draft in a localStorage overlay (never merged into the report); the
  hint text recommended `main.py`. Owner decisions: (1) the template is served by
  a Download button in the panel after the run, with no server slot; (2) a strict
  name `sl_assigned_reasons.xlsx`, downloaded STRAIGHT under that name (`download_from`
  renames the write-back output on download); (3) the template is FULL — all
  under-delivery rows, already assigned reasons filled in, unfilled ones
  first: the file is self-accumulating, so the analyst's work is not lost when
  a fresh template is downloaded; (4) after success — a panel (Download + "Show
  the new report"); no more auto-reload. A special run = a full
  recompute (data folder + reasons-file folder; the warm OPFS cache makes it
  cheap); a copy of the reasons file goes to the server as a run artifact.
  **Platform:** the manifest key `aux_inputs` (≤3; strict names without `..`;
  a button label; `download_from` must reference a `write_back` slot) —
  validated and frozen like everything else; a second button + a separate read-only
  FSA handle (`aux:{dash}`); the reasons file is required BEFORE start
  (a diagnosis card, not a crash), it overrides a same-named file in the
  main folder, counts towards the C.6 boundary and goes through the same
  `add_file` → `"service"`; `POST/GET …/runs/{id}/aux` (10 MB, the name
  is checked against the frozen manifest of the RUN's version,
  `uploads/_generators/{gid}/runs/{run_id}/`); the run history carries
  `aux_files` behind a substring prefilter before `json.loads` (measured:
  19.6 → 2.6 µs/row on a 100-file listing). A hang was closed along the way:
  after a write-back failure the Run button stayed hidden (`running` was reset
  bypassing `setBusy`). **slreport 2.1.0:** full template (the quality counter
  still = the number of unfilled rows); the whole
  localStorage draft was removed from the frontend (−341 lines); the client-side "Export XLSX" button
  reproduces the full template from slot 0 and downloads it as
  `sl_assigned_reasons.xlsx`; the Python core did NOT change — `sl_assigned_reasons`
  was already service/override: the R2 contract survived the extension without touching the
  generator. **Gates:** platform smoke 70/70 (20 aux checks were written by
  Haiku in parallel and independently converged with Sonnet's implementation on the
  frozen schema — reproducibility of the contract); SL equivalence
  ALL PASS (slot 0 byte-identical, snapshot data identical, template = all
  103 detail rows, the round-trip merged a filled row, 5→4 unfilled);
  pytest 14/14; the real zip accepted by the registration pipeline (201,
  aux_inputs frozen, openpyxl `pypi-pure` + a wheel from our origin).
  Bundle `slreport-bundle-2.1.0.zip` sha256 `aa4d9f08…2045`, the rebuild is
  deterministic. A guide for author teams was written:
  `docs/GENERATOR_AUTHORING.md` (boundaries, contract, cache, performance
  discipline with measured numbers, acceptance gates).

- **2026-07-21 — MULTIPLE FOLDERS + `required_files`: data lives in
  several trees.** Trigger: the fifth live run — the first SL computation
  needs at least two folders (weekly exports + `SL_NWD.xls` on the network
  share `H:\group\…\SL NWD\`), while the engine held a single FSA handle; the reasons
  catalogue had to be copied into the data folder by hand, and when it was missing the
  report was SILENTLY computed on the fallback catalogue (reasons.py). Owner decisions:
  a free list of folders + an optional manifest key `required_files`
  (a hybrid — "the error is caught in a second, not on file 130"); write permission —
  a checkbox when adding each folder; one name in two folders — a diagnosis
  before start, no silent precedence. **Platform:** `required_files`
  (≤8 strict names, the aux mask, no overlap with `aux_inputs`) is validated
  and frozen into the immutable version; the engine — up to 7 additional
  folders (main + 7), one IDB key `extra:{dash}` with an array of
  `{handle, w}`, an enumerate union with a `dir` tag on each file, the worker
  opens via `dirHandles[f.dir||0]`; pre-start checks in order:
  aux override → duplicate names (one Map) → `required_files` (folders ∪ aux) →
  the C.6 boundary over the union; write-back goes to the main folder + every
  write-enabled additional one, per-folder failures are non-critical (a hint
  in the success panel). A single-folder run produces a byte-for-byte unchanged
  `input_manifest` (`dir` only when >0, `dirs` only when there are
  additional ones) — the `aux_files` substring prefilter is unaffected. Folders
  are deliberately NOT visible to the generator: the contract is a flat stream of names;
  "stocks/reasons" are told apart by name, not by folder (paths are unstable
  between machines, names are stable). **slreport 2.2.0:** manifest only
  (`required_files: ["SL_NWD.xls"]`), not a line of Python. **Gates:** smoke
  81/81 (+11 required_files checks, Haiku independently converged with
  Sonnet's implementation); the real zip accepted, 201, `required_files` and
  version 2.2.0 frozen; the rebuild is deterministic; node --check is clean.
  Bundle `slreport-bundle-2.2.0.zip` sha256 `067d49d0…8c75f` (98747 bytes).
  Deploy order: the server FIRST (otherwise an unknown manifest key → 422),
  then the bundle. The guide and CLAUDE.md were updated. Same-day postscript:
  the real folder reached 106 files — the max_files ceiling was raised 100 → 500
  (owner decision; the binding constant is still in bytes — 500 MB;
  the only cost of the count ceiling is the walk, ~63 ms/file over SMB ≈ 30 s at
  the full 500). slreport 2.2.1 = `max_files: 500` in data_scale, sha256
  `b150c75a…23dc`; for engine=auto registration does not go through browser_blockers,
  so the bundle is accepted even WITHOUT a server redeploy — the server CEIL of 500
  ships with the next deploy.

---

## Appendix C. Phase 1 — topology matrix (2026-07-20)

Criterion weights (order from §7, descending): CR1 correctness+E2E on P1/P2 = **5**;
CR2 cost of a new generator = **4**; CR3 security/operations = **3**;
CR4 peak RAM on the user's machine = **2**; CR5 UX friction = **1**. The numeric weights
(5/4/3/2/1) are an *assumption* that instantiates the stated order; the order itself comes from §7.
Cell scores 1–5 (5 = better) are judgement tied to the measured numbers of
Appendices A/B; the ordinal ranks are stable, the specific integers are a modelling choice.

Measured reference numbers (target machine = the work laptop, 2 cores / 4 GB, IT-4;
derating relative to the dev Mac ×10.0–10.4, S1b): parse per file (~420 thousand cells) — native
calamine **1.82 s** (S1b), Pyodide+calamine **3.3–4.2 s** (App. B), SheetJS
≈ 2.8 s (extrapolation), Pyodide+openpyxl ≈ 13–14 s; I/O from the share (S2) — year
≈ 10 s, current folder ≈ 5 s, incremental ≈ 0.15 s; tab ceiling **2.75 GB**
(S4); Pyodide rss ~887 MB (App. A); SheetJS 0.18.5 — unpatched advisories.

### C.1. Summary table of candidates × criteria

| Candidate | CR1 (×5) | CR2 (×4) | CR3 (×3) | CR4 (×2) | CR5 (×1) | Σ weighted |
|---|---|---|---|---|---|---|
| **A** browser Pyodide | 3 | 4 | 4 | 2 | 5 | **52** |
| **B** browser JS port | 2 | 1 | 2 | 3 | 5 | **31** |
| **C** hybrid agent+conductor | 5 | 5 | 3 | 4 | 3 | **65** |
| **D** launcher-only + API tokens | 4 | 5 | 2 | 4 | 2 | **56** |
| **E** server-side run | 1 | 5 | 1 | 5 | 1 | **39** |
| **F** A+C behind the R2 contract | 5 | 4 | 3 | 3 | 5 | **61** |

Rationale for the key cells: **CR1** — A=3: P1 works (narrowed cold 125–145 s within the
threshold), but P2 is physically ruled out (2.75 GB, S4); C=5/F=5: cover P1 and P2;
D=4: the computation is complete, but tokens conflict with C5 and there is no browser UI (G2); E=1: violates
§1. **CR2** — B=1: a port of every script (C2); A=4/F=4: Python once, but with
a Pyodide tax (wasm32 wheels; openpyxl only via micropip — App. A);
C=5/D=5: native Python with no tax. **CR3** — A=4: neither server-side execution
nor a localhost listener; C=3/F=3: the agent (PNA/pairing, K2) + a native process;
D=2: a token on every machine (exactly what R4 fixes); E=1: executing code on the
server breaks the OnePage model. **CR4** — A=2: 887 MB of wasm base on a 4 GB machine;
C=4/D=4: native, streaming; F=3: P2 with the agent, P1 squeezes the tab. **CR5** —
A=5/B=5/F=5: zero installation, one persistent prompt (S4); C=3: installing/pairing
the agent; D=2: the G4 pain remains.

### C.2. Eliminating the dominated

- **B — dominated by A**: A is strictly better on the three senior criteria (CR1 3>2 without
  forking the logic; CR2 4>1 — the pre-declared C2 domination; CR3 4>2 — SheetJS
  advisories). B's nominal edge on CR4 (3>2) does not give a capability that A
  lacks (the tab ceiling is shared). Σ 31 is the table minimum. Out.
- **D — strictly dominated by C** (Pareto: C ≥ D everywhere, strictly better on CR1/CR3/CR5).
  In substance C = D plus a browser conductor; D gives up live `op_session`
  publishing for tokens that C5 explicitly postpones. Out.
- **E — out for violating the hard constraint of §1** ("raw data is never uploaded
  to the server"), not just on points: its only advantage (CR4) cannot be realised
  within the constraints — to relieve RAM you would have to send those very
  gigabytes to the server.

Finalists: **A, C, F** (F ⊃ A and F ⊃ C by construction).

### C.3. Weighted analysis of the finalists

Raw weighted rank: **C (65) > F (61) > A (52)** — by the literal §7 weights the
agent-first C beats the two-engine F. All of F's advantage is concentrated in CR5
(the lightest criterion); F loses to C on CR3 (a second engine + the Pyodide
supply chain) and CR4, and computationally the browser engine on the binding machine is
1.8–2.3× slower than the agent (3.3–4.2 vs 1.82 s/file).

The key difference: in C every user must install the agent; in F
the browser computes P1 itself → zero installation. F hits the primary pain of the
baseline directly (G4 "too complicated"); C does not. Meanwhile the launcher already computes natively and
nobody has complained about speed — the adoption bottleneck lies in CR5, not CR1,
i.e. "adoption" in this project effectively carries the weight of a binding constraint, not
of the lightest criterion. **C wins the literal matrix; F wins under a
defensible re-weighting of UX/adoption — and that re-weighting is the substance of the
owner's registered decision, not a consequence of the raw matrix.**

### C.4. Attempt to refute F — the three strongest counter-arguments

1. **The cost of two engines (CR2/CR3).** Two runtimes forever: pinning Pyodide
   (314.0.2 / ABI 2026_0 — a moving target), openpyxl outside the distribution,
   being hostage to wasm32 wheels existing (calamine has one today — not
   guaranteed tomorrow); a dependency without a wasm wheel silently makes a generator
   agent-only. *Analysis:* the agent gets built in any case (CA-3), so the
   marginal cost of F over C is only the Pyodide engine, amortised across the
   C2 family. Weakens it, does not kill it.
2. **On the binding machine the browser is slower than the native agent (CR1).** Cold year:
   agent ~101–110 s (passes), browser 185–240 s (fails — which is why P1 was narrowed).
   Cold current folder: agent ~52–62 s, browser 125–145 s. *Analysis:* in the
   dominant weekly-incremental scenario the parsing penalty is barely visible (one file,
   3.3–4.2 s against a 15 s budget); cold is paid once per machine (the OPFS cache is
   the owner's condition #1). Accurate for the cold year, not for the incremental. F survives
   with a narrow P1 + a mandatory cache.
3. **P2 requires the agent anyway → Pyodide is additive; just build C (CR1/CR4).**
   The 2.75 GB wall is measured; 30–35 MB giants are already in real folders; K9 (×5–10 for
   dtype=str) brings even the 95 MB year close to ~1 GB. Since the agent is mandatory for P2,
   it can take P1 too: faster and without the memory cliff. *Analysis:* the strongest
   blow; it cannot be refuted with compute numbers — only with adoption/UX numbers (U1).
   It loses only if UX weighs as much as a binding constraint — which is exactly what
   the owner asserts.

**Result: F holds, but conditionally** — the justification for the browser engine rests
entirely on UX/adoption; it is acceptable only in the form "a browser engine behind the same
contract as the agent, built from day one" — never browser-only.

### C.5. E2E calculations for P1 on the target machine (2 cores; * = estimate/budget)

| Scenario | Browser (Pyodide) | Agent (native) |
|---|---|---|
| Cold, current folder (26 files) | ≈ **105–145 s** (within ≤180) | ≈ **58–62 s** |
| Cold, full year (50 files) | ≈ **185–240 s** — fails | ≈ **105–110 s** — passes |
| Weekly incremental (warm OPFS) | **6–12 s** warm tab / 16–32 s cold boot* | **4–7 s** |

The agent passes both cold profiles and carries no boot risk; the browser passes only
the narrowed cold profile and, for the incremental, depends on the unmeasured boot (condition #2).

### C.6. Verdict and the engine boundary

The matrix does not overturn the owner's decision; the decision holds **provided it is
implemented as F** (engines behind the common R2 contract, the agent built from day
one) and with a mandatory OPFS cache. An honest caveat: the literal weights give
C > F; F's lead is the owner's deliberate re-weighting of adoption.

**The browser engine is the default when ALL of the following hold:** ≤ ~30 files per run;
a working set of ≤ ~500 MB raw (and a file ≤ ~35 MB) with incremental aggregation;
a machine no weaker than the target (≥2 cores, ≥4 GB); every dependency has a wasm32 wheel
in the pinned Pyodide. **The agent is mandatory if ANY of the following is true:** a P2 profile;
a working set > ~500 MB or close to 2.75 GB; > ~30 files cold on a weak
machine; a dependency without a wasm wheel; any of the owner's registered
triggers has fired (App. B).

### C.7. Assumptions

The numeric weights 5/4/3/2/1 and the integer cell scores are a modelling choice (the order comes from
§7); Pyodide boot of 10–20 s and agent start of 2–3 s are estimates; the wasm overhead of 1.8–2.3×
is carried over from the dev Mac; publishing in 2–5 s is a budget; the SMB year of ~10 s is a projection;
the ×5–10 inflation (K9) was not measured on these files; rss 887 MB is an order of magnitude from
Node; SheetJS ~2.8 s is an extrapolation; the "~30 files / ~500 MB / ~35 MB" boundaries are
an engineering roll-up of the measured points, to be refined by a micro-test in real Edge.

### C.8. Postscript: U1 cancelled (2026-07-20, owner decision)

U1 (analyst interviews) was cancelled by the owner. A registered gap in the
evidence: the UX/adoption re-weighting — the only basis for prioritising
the browser engine per C.3/C.6 — remains the owner's judgement without primary
data. Compensation: the "pilot user complaints" trigger (App. B) effectively
makes the pilot itself the source of U1 data — collect feedback in the first weeks of the pilot
deliberately (cycle time, where people got stuck, what was unclear) rather than waiting for
complaints. The questionnaire is kept (`drafts/U1_questionnaire.md`) in case it comes back.

---

## Appendix D. Phase 2 — micro-design of the browser engine (2026-07-20)

Target machine: 2 cores / 4 GB, Edge 149, derating ×10.0–10.4 (S1b, IT-4).
Provenance: (S1b/S2/S4/A/B/C) — measured; "estimate" — a calculation, not a measurement.

**The central tension that defines the whole micro-design.** The pilot `slreport`
today reads all the weekly files and does a `pd.concat` of the whole year
(`slreport/dataload.py:93`) + `raw.copy()` (`:97`) with `dtype=str` — trap K9
for the whole year at once. Consequence: **the mandatory OPFS cache (App. B, condition #1)
and per-file memory discipline are incompatible with running an unmodified
slreport on top of a bulk `mountNativeFS`** — the cache needs the boundaries of each
file's contribution. Per-file streaming was chosen; the price is a minimal engine-agnostic refactor
of slreport's entry point (see D.1/D.5).

### D.1. Execution architecture

Main thread — UI/picker/progress only (long tasks < 50 ms, §5); all compute —
ONE Worker with Pyodide (2 cores — nothing to parallelise over, IT-4). The FSA directory
handle is cloneable: the picker and `requestPermission({mode:"readwrite"})` are
main thread + user gesture only; after the grant the handle is posted to the Worker, where
reads/writes work without a gesture (S4: a persistent grant via IndexedDB
confirmed). File bytes are not passed between threads (the Worker reads them itself);
only the result is transferable (an HTML string + a slot `ArrayBuffer`).

**mountNativeFS rejected in favour of a manual per-file loop:** K6 is not resolved
(cost/semantics not measured, write buffering requires syncfs), the synchronous
`createSyncAccessHandle` is available only on OPFS (not on a user
folder), and the manual loop gives O(1) memory and natural boundaries for the cache.
Writing `sl_unassigned_reasons.xlsx` back goes through `createWritable()`;
slreport already writes tmp+`replace` (`reasons.py`) → terminate leaves no
fragments behind. A quick "first light" on the narrowed P1 (26 files) is acceptable on
a bulk `mountNativeFS` without the refactor (it fits in memory, D.3) — a springboard,
not the target architecture.

### D.2. Incremental OPFS cache

**What is cached is the per-file AGGREGATE, not the raw parse:** a groupby-sum over the 13 dimensions of
`_AGG_KEYS` (`packing.py`; associative — it sums across files) + only the file's
shortfall rows + its contribution to meta. The raw `dtype=str` frame (~17–25 MB/file,
estimate K9) must not be cached — the warm path would again run into the year concat.
**Key:** `sha1(schema_version | filename | mtime_ms | size)` — the same trick
as the platform's render fingerprint; `schema_version` = the immutable generator version
(SHA) → a code change = full invalidation = one cold re-parse.
**Format:** the columnar `pack_slot` contract is reused (§8 "do not change") +
a JSON side-car for meta; stored in OPFS via `createSyncAccessHandle`
(synchronous access in the Worker). Quota: `storage.estimate()` before writing,
`QuotaExceededError` → LRU eviction → retry → degrade to no-cache (speed suffers,
correctness does not). **Gain:** cold year 185–240 s (App. B) →
warm incremental 6–12 s warm tab / 16–32 s cold boot (C.5).

### D.3. Memory discipline under the 2.75 GB ceiling (S4)

Peak per file: Pyodide base ~887 MB (A) + frame ~20 MB (estimate K9) + a small
aggregate ≈ **~910 MB, O(1) in the number of files**. The current concat path (estimate):
26 files ≈ 0.5 GB + `copy` 0.5 GB + base 0.887 ≈ **~1.9 GB — fits**;
50 files ≈ **~2.9 GB > 2.75 GB — the full year fails on MEMORY, not
only on time** (numerically confirms the C.6 boundary). Discipline: read →
normalize → contribute to the running aggregate → shortfall into the running detail → `del` +
`FS.unlink` + a periodic `gc.collect()`; downcast dimensions to categorical,
numbers to i32/f64. **Monitoring: `performance.memory` does NOT see the wasm heap** —
track `WebAssembly.Memory.buffer.byteLength`, catch a RangeError/abort on
growth and return "an agent is needed" instead of crashing the tab;
`measureUserAgentSpecificMemory` requires crossOriginIsolated — not used.

### D.4. Boot strategy

Pin 314.0.2 / ABI 2026_0. Recommendation — self-host pyodide+wheels in OnePage's `static/`
through the existing `CachedStaticFiles` (?v=-immutable; precedent —
self-hosted fonts; K5/K10), with the CDN as a working fallback (IT-2). The self-host must
include the PyPI wheels (openpyxl is outside the distribution — App. A). Warm-up: a Service
Worker asset cache + preloading when the Data dock opens, before the "Run" click;
the download is removed by the HTTP cache, **wasm compilation is CPU-bound** — mitigated by the
Chromium code cache on a stable URL. Warm boot is not measured (estimate
10–20 s cold) — the fate of the 15 s incremental budget is decided by the
`pyodide_boot_test.html` micro-test (condition #2).

### D.5. Running the generator

The immutable version's bundle — fetched from OnePage → unpacked into the Pyodide FS →
`sys.path`; dependencies via micropip from the self-host, validated against the manifest
(no wasm32/pure wheel → the generator is agent-only). Entry contract:
`run(file_iter, out_dir, progress)` instead of a CLI — the same hook for the agent (R2),
not a fork of the logic (C2). **Findings:** brotli compression is unavailable in the browser
(CompressionStream — gzip/deflate only), but slreport falls back to
gzip as designed (`packing.py`), and the platform serves `.gz` with the correct Content-Encoding —
not a blocker; xlrd (the `.xls` reasons catalogue) is pure Python or is replaced by calamine.
stdout/tqdm: `setStdout(batched)`; tqdm is silenced (`disable=True`), progress comes
from an injected callback (TTFR < 1 s, update ≤ 500 ms). Cancellation =
`Worker.terminate()`: the reasons file is atomic, publishing had not started (R6),
the OPFS cache survives → a rerun is warm on data. K11 filters — in JS
before parsing (`~$*`, `._*`, `__MACOSX/`, empty files), skip+report.

### D.6. The engine boundary at runtime

Before launch: a folder walk (~63 ms/file, S2) + the K11 filter → `file_count`,
`total_bytes`, `max_file` + deviceMemory/hwConcurrency + wasm availability of the
dependencies → checked against C.6. A violation → not launch-and-crash, but a diagnosis card
with numbers ("The folder has N files / X MB, the browser engine's limit is ≤~30 / ≤~500
MB / ≤~35 MB per file; an agent is needed") and an action. The C.6 boundary is enforced
technically, not by user discipline; the 30–35 MB giants in real folders
(App. B) will trigger the check.

### D.7. Pilot telemetry (op:gen:* → perf.js → /api/perf/client)

Stages: `boot`, `deps`, `bundle`, `enumerate`, `cache:hit/miss`, `parse`
(+TTFR), `aggregate`, `reasons`, `pack`, `publish`, `total`. Gauges:
`peak_wasm_bytes`, `files_parsed/cached`, `border_decision`, `enc`. These numbers
close the caveats of the gate failure (boot, wasm overhead, full-year memory) with data from the
first weeks of the pilot — the pilot also serves as the source of U1 data (C.8).

---

## Appendix E. Phase 2 — the platform side: manifest, API, permissions, UI (2026-07-20)

The principle of this section: not a single parallel mechanism — every item occupies
an existing node of the platform (verified by reading the code; `is_published`
was additionally verified by the main session: `models.py:106`,
`dashboards.py:91`, the draft badge `themes.html:49`).

| Needed | Occupies an existing node |
|---|---|
| Immutable code versions | the `storage.py` pattern: `v{n}/` + a `current` pointer |
| Atomic publishing | the `replace_html` commit order: files → one `db.commit()` moves the pointer |
| Display invalidation | `version_fingerprint()` + render LRU + `warm.schedule()` — as usual |
| Draft (R5) | the existing `Dashboard.is_published=False` — already hides it from everyone except owner/admin |
| Permissions | `ROLE_PERMISSIONS` + `can_update_dashboard_data`; ONE new key `manage_generators` (admin-only) |
| Audit | `AuditLog.set_detail()`, new action strings |
| UI launcher | the `/themes` pattern → `/d/{id}?dock=data` |
| Data into the dashboard | the existing `op_ready`/`op_file` (slot 0) |

### E.1. Generator manifest

`manifest.json` at the root of the bundle/repository; validated ON THE SERVER at
registration (an error stops the admin, not the analyst). Fields: `name`
(a stable key), `version` (the author's label; the platform numbers
`version_number` itself, not trusting the client), `entrypoint.module`
(`python -m …`; the fixed flags `--input-dir/--output-dir/--params-json`
are added by the engine — one calling convention for both engines, R2), `params[]`
(a description of the UI form: key/type/label/default/required), `deps[]` (name+version;
the attribute `wasm32: bundled|pypi-wheel|pypi-pure|unavailable` is **computed by the
server** against the pinned Pyodide 314.0.2 lock + a PyPI snapshot at the time of
registration and frozen into the immutable version), `inputs[]` (declared
input data — pattern+role+sheets+columns: `{match: glob, role: data|reference,
required: bool, sheets: [{name: glob, columns: [str]}]}`; up to 32 entries,
the pattern WITHOUT `/` or `..`, case-insensitive; when non-empty, the engine reads
ONLY the matching files), `checks[]` (data-quality checks: `{match, sheet, column, type, ...}`
of types `not_null|unique|range|regex|enum|known_values`; up to 64 entries; executed
in the browser before publish, the results feed into Data Quality; full description —
see GENERATOR_AUTHORING.md, section "Quality checks"), `outputs`
(`html: bool` — false means "update only the slots on top of the current HTML";
`slots[]` with index/name/type and `write_back` for files returned to the user's
folder — `sl_unassigned_reasons.xlsx`), `data_scale`
(max_files ≤ 30 / max_total_bytes ≤ 500 MB / max_file_bytes ≤ 35 MB — the ceilings
from C.6; an `engine=browser` manifest with numbers above them is rejected at registration with
422 rather than failing for the analyst — **cancelled 2026-08-20**: the numbers became
a recommendation, registration no longer checks them, and the engine warns the
analyst and offers "Run anyway"), `engine: browser|agent|auto`,
`write_back: bool` (a readwrite FSA grant instead of read).

The slreport example: deps pandas 3.0.3 / python-calamine 0.6.2 (`pypi-wheel`,
App. A) / openpyxl 3.1.5 (`pypi-pure`, outside the distribution — App. A); inputs
with the patterns Stock_*.xlsx (role data) and SL_NWD.xls (role reference); outputs
html + slot0 `sl_bc_data.bin.gz` + slot1 write_back `sl_unassigned_reasons.xlsx`;
engine auto.

### E.2. Data model

Three new tables + three nullable columns in `data_sources` + one in
`generation_runs` + one nullable FK in `dashboards`; the other tables do not change:

- `generators`: id, name (unique), is_archived, git_repo/branch/token
  (**token write-only** — GET returns only `git_token_set: bool`),
  git_last_synced_sha/at/error, created_by/at.
- `generator_versions` — **immutable (R7)**: generator_id, version_number
  (monotonic), source_type (bundle|git), bundle_path
  (`uploads/_generators/{gid}/v{n}/`), manifest_json (a snapshot with the computed
  wasm32), git_commit_sha, is_current, checksum_sha256, created_by/at.
- `generation_runs`: dashboard_id, generator_version_id (which version ACTUALLY
  executed — the R4b discipline), engine, status
  (running|success|error|canceled|lost), started_by/at, heartbeat_at,
  finished_at, input_manifest (JSON: files+sizes+mtimes), error_message/detail,
  output_version_number, duration_ms, bytes_published, **outputs_json** (write-back slots
  with the publish result: [{name, targets, failed}]).
- `data_sources`: + **version INT**, **first_seen_at/last_seen_at DateTime**,
  **fingerprint VARCHAR(64)**, **last_file_count INT** (folder identity —
  basename + a fingerprint of the set of names; stability tracking).
- `source_observations` (new): run_id, data_source_id, dir_index, file_count,
  total_bytes, newest_mtime, fingerprint, skipped_json (reasons for skips),
  truncated, created_at. UNIQUE(run_id, data_source_id).
- `source_files` (new): observation_id, name, size, mtime_ms, kind
  (listed|parsed|cached|service|unreadable|excluded|legacy), matched_input (the pattern from inputs),
  schema_json (header, sheets — sniffed before add_file), consumed_json (what was actually read).
- `input_overrides` (new): generator_id, data_source_id, exclude_rules JSON,
  version INT, updated_by, updated_at, created_at. UNIQUE(generator_id, data_source_id).
  Exclusion rules on the (generator, source) link instead of on the folder (a new R-decision).
- `dashboards.generator_id` — a nullable FK (the pilot is 1:1). A run takes the CURRENT
  generator version (as the launcher takes the branch); the run records the version that executed.

**Migration from v1** (`app/services/lineage_migrate.py`): two one-off idempotent operations
run on every boot. (1) The old `exclude_rules` on a
`data_sources` row (folder-level, applied to every generator) become
rows in `input_overrides` on the (generator, source) pairs from the run history —
each generator needs to know which sources it may read and with which rules.
Folders with no runs stay where they are; a warning names them. (2) A backfill of observations from
the old `generation_runs.input_manifest`, so the new page has history on day
one. They are marked `kind="legacy"`: they carry name/size/mtime from the manifest, and the sniffed structures
are absent (the old system did not sniff).

**Declared vs observed** (`app/services/schemadiff.py`): `genmanifest.inputs_for()`
tells what the generator DECLARED it reads (pattern, sheets, columns); `source_files.schema_json`
from `static/py/_lineage.py::sniff` — what it actually SAW before `add_file` (sheets and header
via calamine). `diff()` compares the two: a pure function with no DB; it works at
publish time (this run) or later on any {inputs, files} pair — **the seed for Data Quality**.

**Draft (R5) — for free:** `is_published=False` already hides the dashboard from
viewers and grantees (the filter at `dashboards.py:91`); owner/admin see it.
The CTA placeholder is not a branch in rendering but **the content of v1**: the create flow saves
a platform template ("The report has not been generated yet — open Data and run it")
with the same `storage.save_html`. The first successful publish sets
`is_published=True` automatically: only someone who can fix the emptiness sees it;
once fixed, everyone entitled to see it does.

### E.3. API

**Code registration (the `manage_generators` right, admin-only):**
`POST /api/admin/generators` (400 manifest/size, 409 name, 422
engine=browser with an unavailable dep); `POST …/{id}/versions`;
`PUT …/{id}/git` (503 when `OP_GENERATORS_GIT_MODE=false`; token write-only);
`POST …/{id}/git/sync` (424 when GitHub is unavailable = **degradation to
"the last snapshot vN"**, the pointer does not move); `PATCH …/{id}` (archive;
no hard delete). A lightweight `GET /api/generators` (the `upload_dashboard` right) —
for picking one at creation and for the provenance line "slreport v3 · admin1 · date" (R7).

**Bundle for the engine** (the `can_update_dashboard_data` right):
`GET /api/dashboards/{id}/generator/manifest` and `…/bundle?v={version}` —
the version is immutable → `Cache-Control: immutable` when `?v=` matches
(the same trick as `snapshot.js?v={fp12}`); the engine caches the bundle in OPFS.
`…/manifest` — +`source_rules` only from this generator's `input_overrides` × the dashboard's sources,
+normalised `inputs`, +`force_deps: ["python-calamine"]`.

**Run lifecycle** (the `can_update_dashboard_data` right):
`POST …/generator/runs` (409 on a live run-lock, 413 when the manifest is > 1 MB or files > 5000
or folders > 8; 400 on C.6 boundary violations) — **run-lock**: a live `running` within the TTL
(`OP_GENERATORS_RUN_LOCK_TTL_S`) → 409 `{locked_by, started_at, age_s}`;
`engine` is resolved (auto → manifest + the C.6 boundary); +`observation_ids`, +`last_known {name:[size,mtime_ms]}`.
**`POST …/runs/{id}/observations`** (new; 400/409 on seq, 413 when > 2 MB,
also accepted for error/published runs for 60 s after finished_at) — accepts `{seq, files, final}`
from the engine; seq is monotonic (a duplicate → 409); final → fingerprint + audit.
`PATCH …/runs/{id}` — heartbeat/progress (extends the lock).
`DELETE` — cancel. `POST …/error` — a human-readable error.
`POST …/publish` — atomic publishing; optionally `write_back[]` → `outputs_json`;
`publish_warnings` + a header diff (schemadiff).
`GET …/runs`, `GET …/runs/{id}` — history/details with observations.
**Lost locks — no sweeper thread:** the next POST itself declares a record older than the TTL `lost`.

**Overrides CRUD** (the `can_update_dashboard_data` right, reach check via `_generator_dashboards`):
`PUT /api/data-sources` (version; a mismatch → 409);
`GET/PUT /api/data-sources/{id}/overrides[/{generator_id}]` (scope — the generators the user may run and that have ever read this source) — patterns validated via `globmatch.validate_pattern`.

**Lineage** (new, the `can_update_dashboard_data` right): **`GET /api/data-sources/lineage`**
(2026-08-20) — `{nodes:[{id,layer 0..5,kind,label,data}], edges:[{from,to,kind}]}`; 6 layers:
folder→pattern→sheet→generator→output→report; columns are data inside the sheet nodes, not a separate layer;
a legacy generator gets a synthetic `*` node. Lineage is built from the `source_observations` of the latest period;
bytes = the sum of `source_files` of the latest observation (double counting disappears).

**Atomic publishing (R6)** = the `replace_html`+`update_slot_file` code in one
transaction: (1) `v{n+1}` lands on disk in full BEFORE any DB change (html or a copy from
`v{n}`, all slots); (2) one `db.commit()`: DashboardVersion + DashboardFile +
`current_version=n+1` + recomputing dash_type/slot_count/render_size +
`run.status='success'` + the first success → `is_published=True`;
(3) `warm.schedule()` after the commit; the fingerprint rotates by itself (new mtimes),
SWR covers the boundary moment. Limits: data — the existing
`_MAX_UPLOAD` of 50 MB; code — a new `OP_GENERATORS_BUNDLE_MAX_MB`.

**Admin overview:** `GET /admin/generators` (the `view_admin` right, a tab in the shared
admin panel) — versions + failed runs by dashboard/owner, the silhouette of
`/admin/errors`, sourced from `generation_runs`.

### E.4. Permissions and audit

`manage_generators` → `ROLE_PERMISSIONS[ROLE_ADMINISTRATOR]` (the same mechanism
as `manage_any_dashboard`). Run/publish/history —
`can_update_dashboard_data` (owner, admin, the `can_update_data` grant) — the function
does not change. New audit actions: `create_generator`, `generator_new_version`,
`generator_git_register` (without the token value), `generator_git_sync` (SHA or
degradation), `generator_archive`, `create_generated_dashboard`,
`generation_run_start/cancel/error/publish` (version, bytes, run_id,
generator_version).

### E.5. UI

**The viewer Data dock** (an existing tab): a generator section — provenance
("slreport v3 · admin1 · date"), "Choose folder"/"Refresh from folder"
(a persistent FSA handle, S4), an engine indicator, progress from the `op:gen:*` marks
(the existing perf.js beacon, no new endpoint), a card for the last
run (status/error with an action, R8). Manual slot upload remains for
edits between runs; publishing is always a batch through `.../publish`.

**The lineage page** (`data_sources.html:…` or a tab in the Data dock): a DAG
with 6 layers (folder → pattern → sheet → generator → output → report); columns are shown as data inside the leaves;
nodes are grouped by type, edges show "read"; column chips come in three colours
(declared+observed / declared-only / observed-only); collapse/expand of pattern→sheets→columns,
zoom/pan, click-to-focus (BFS closure, addressed as `?focus={node_id}`). The drawer is
an editor of `input_overrides` per generator, with pattern validation and 409 handling.
Source bytes = the sum of `source_files` of the latest observation (double counting disappears).

**The `/generators` launcher** is literally `/themes`: a list of generator-backed
dashboards, "last run: date/status", a click → `/d/{id}?dock=data`
(the existing `viewer.js` deep link). **Create flow**: "title + generator"
→ `POST /api/dashboards/generated` → a draft + the CTA template as v1 → redirect to
the Data dock.

### E.6. Fault tolerance

A single invariant (inherited from `storage.save_html`/`_current_file`, not
extended): **a version is a directory that is either fully in place before the DB
points to it, or does not exist for readers.** A break during bundle upload /
git sync / computation in the tab / the publish upload — in every case
the single commit point (`db.commit()`, which moves the pointer) either does not
happen, or happens in full; rendering and the fast lane read
`current_version`, and a half-written `v{n+1}` is never requested by anyone. A double
run — 409 at start (before publishing); a hypothetical publish race —
"the last commit wins", version numbers are monotonic.

**Honest limits of observations (2026-08-20):**
- **Folder identity** remains basename + fingerprint (sha256 of the sorted,
  case-folded set of names; two reads of the same folder with different fingerprints
  are different folders as far as lineage is concerned, even if the path is the same).
- **The cache blind spot:** files with status `cached` (found in the cache by name/size/mtime)
  are not opened by the generator, so the hooks are blind; the platform shows "consumed: as of run #N"
  and takes the structure from the previous observation. Header sniffing still works
  (it runs BEFORE `add_file`, while the file is still on disk).
- **The agent engine** (GreenPlum): a SQL subprocess, no hooks, declared lineage only.
- **Backfill from v1:** the `lineage_migrate.py` migration fills the observation tables from
  the old `input_manifest` field; these observations are marked `kind="legacy"` and carry
  file metadata but not sniffed structures (the old system did not sniff). They
  are older than any new run, so new observations displace them; the lineage graph
  always uses the LATEST ones.
  

### E.7. Open doors

API tokens C5 (an `api_tokens` entity modelled on the write-only `git_token`, a
`run_via_api` right, Bearer as an alternative to the cookie — the publish pipeline does not change);
schedules (need tokens; `started_by` may become a nullable system value);
a run console (after schedules); multi-dashboard generators (moving
`generator_id` → an M:N table, deliberately not built).
