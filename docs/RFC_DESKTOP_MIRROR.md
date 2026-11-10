# RFC: desktop report mirror and local runs

Status: **in progress**. Phases 1–2 (server) are implemented; phases 3–5 (syncer,
serving, library, tray) are written, wired into the agent's server, and as of
11.10.2026 are for the first time able to **sync**: before this pass the
`sync.sync_all` entry point did not exist at all, and the mirror had never
updated even once. What still stands and what has NOT been verified is in
"State as of 11.10.2026" at the end; the main unverified thing: nobody has had a
real Windows machine, a real OnePage server, or a browser on the mirror's pages.
Owner decisions of 11.10.2026.
Related documents: `docs/RFC_AGENT_GREENPLUM.md` (the agent itself),
`docs/PERF_DECISIONS.md` ADR-18 (RLS), `docs/RFC_GENERATORS_RESEARCH.md`
(the R2 engine contract), `docs/DEPLOY.md` §backups.

## Why

The owner's request: "servers burn down, a whole application gets lost" — we
need a Windows 11 program that keeps published reports on a person's machine and
lets people with the rights update them locally, without waiting for the server.

**First, what NOT to build, because it is already built.** The platform is
protected against data loss, and better than a desktop program could manage:
`deploy/backup.sh` takes a consistent copy of the database through the SQLite
backup API (correct under WAL), archives all of `uploads/` — that is, every
`dashboard.html`, slots, themes, the lake — and the admin overlays, which take
precedence over env; `deploy/restore.sh` restores with `PRAGMA integrity_check`;
`deploy/restore-drill.sh` once a quarter brings up a **real instance** from the
archive and hits `/health`. This is a procedure proven by drills, not a hope.

What remains uncovered is not durability but **readability**:

1. **The archive is unreadable without the platform.** A report does not open
   from `uploads/` with a double click. It needs injections
   (`detector.inject_bootstrap`), data delivered over the `op_ready`→`op_file`
   bridge, relative assets resolved by their ORIGINAL names, and `.br`
   decompression. No host and no image means no reading.
2. **The archive sits with the operator**, not with the person, and it cannot be
   handed out: it contains live sessions and model keys.
3. **Access hinges on ADFS.** Switch AD off and the restored instance lets
   nobody in.

Hence the framing that justifies this work, and without which it looks like a
duplicate of the backup: **`backup.sh` answers "how do I restore the platform",
the mirror answers "how do I read my report right now, while it is being
restored".** These are different RTOs: a day with an engineer versus zero
without one.

## Owner decisions (11.10.2026, not to be revisited)

| Question | Decision | Consequence |
|---|---|---|
| What we protect against | Server downtime **of days**; the project is alive | The mirror is a fresh working copy, not a permanent archive. **No version history needed** |
| Whose copy | Everyone has their own, only what they have access to | Rights are respected by construction; nobody has a full archive, and that is fine |
| What works offline | The report opens and **works fully** | A local viewer is needed, not a folder of files (see "file://" below) |
| How it lives on the machine | Background sync, like Dropbox | A long-lived key is needed: a session lives 8 hours |
| How it gets onto machines | **A folder, no installation, no admin rights** | The agent's offline kit is reused in full |
| Local run without the server | Must work | The bundle, wheels and rights are cached in advance |
| When the server is back | **Ask the person**, do not publish silently | A catch-up queue with explicit confirmation |

## Decisions made during design

### 1. One program, not a second one — and lifting an agent invariant

We extend the existing `op-agent`. We reuse what is already written and proven
on a live machine: the tray in pure `ctypes` (`agent/tray.py`), the DPAPI secret
store (`agent/creds.py`), the streaming local HTTP server (`agent/server.py`),
safe unpacking (`agent/zipguard.py`), the single-file build
(`deploy/build_agent_single.py`) and the offline kit with a relocatable CPython
and autostart via HKCU (`deploy/make-agent-kit.sh`). Two tray icons are bad for
a person, and there is no point duplicating six hundred lines of `ctypes`.

**But this comes at an explicit price, and it has to be named.** `agent/README.md`
states: "Listens only on `127.0.0.1`, holds no OnePage secrets, **does not talk
to the OnePage server**". The third part of that promise is revoked: the agent
gains outgoing requests to one host — our own platform — and starts holding an
access key to it. What does **not** change: it still listens only on loopback,
still accepts nothing from the network, and the key it holds cannot write by
construction (see below). The first two parts of the promise remain in force,
and the README must be rewritten so that this distinction is visible, not
glossed over.

### 2. Local HTTP on loopback, not `file://`

This is not a convenience but a condition of existence, and it had to be
established before work began. A parent page opened as `file:` **cannot** `fetch`
slot files: Chromium forbids it. And the report iframe lives in a sandbox
without `allow-same-origin`, i.e. with an opaque origin, and cannot request
anything itself — which is exactly why data travels over `postMessage` in the
first place. An interactive report contains no data by construction. So "save
the html, click, read" yields an empty report, and the temptation to go that
way has to be extinguished up front.

The sandbox **must not be touched**: a threat model rests on it (a report is
user-uploaded JS running while an administrator views it).

### 3. Device key: read-only, and enforced by code

Implemented in phase 2: `app/auth/devicetoken.py`, the `DeviceToken` model,
migration 0008, routes in `app/api/me.py`, the check in `app/auth/deps.py`.

Why not the alternatives. A password from `OP_LOCAL_USERS` works only on stands
— production uses ADFS. Embedded WebView2 for SSO needs a person at the
keyboard, which kills background sync. Share links need no cookie at all, but
are issued by hand per report and **do not work for RLS reports** — and refusing
them would have to be explained anyway.

The key does not widen access by a single byte: it only lets a person use what
they already have without a browser. That is why a person issues it to
themselves, without a separate right, and the routes live next to the language
setting, not in the admin area.

**"Read-only" is enforced by a method check, not a list of paths** — any method
other than GET and HEAD is refused. That way a new mutating route ends up closed
on its own. A consequence worth knowing: a key cannot issue a second key and
cannot extend its own access — that always requires a live login.

### 4. Reports with RLS never go into the mirror. Ever.

A report with a policy (ADR-18) **has no file that can be saved**: its rows are
filtered for a specific viewer on every request (`rls.serve_filtered`), share
links for it are already closed (403 in both `_authorize_render` and
`share_view`), and `/source` is closed to non-`see_all` users. A copy on disk
is someone's filtered rows lying around unfiltered, which is exactly the leak
ADR-18 forbids.

So `GET /api/sync/manifest` marks such a report `mirrorable: false` with reason
`"rls"`, and the program must skip it. **It does not disappear from the list**:
the person should learn why the report is not in the mirror, rather than
conclude it got lost. A local run of an RLS report is forbidden to everyone
except `see_all` — the server makes that check in `_assert_can_update_data`, and
locally it is easy to forget.

### 5. A carried-over right is a right that cannot be revoked

On the server, revocation is instant: the row is deleted, the next request gets
403 (even the fast lane lags by no more than a second). For a local copy,
revocation arrives only with the next successful sync, and code cannot fix that.
Policy can: a carried-over right has an expiry (`rights_ttl_s`, one day), after
which **local updating** is blocked, but not reading. A person is entitled to
read what has already been downloaded even on the third day of downtime —
otherwise the whole idea loses its point.

## What the server provides (phase 1)

`GET /api/sync/manifest` — one response from which the program works out what is
stale on its side. Before it, the list of available reports was returned only as
the `#dash-data` blob inside the home page HTML: parsing HTML as an API breaks
silently when the template is edited, `upd` there is accurate **only to the day**
(three edits in a day are indistinguishable), and slots require a request per
report.

A separate route, not an extension of the index: the home page has a budget of
six queries, written down in its own comment.

**`fingerprint` closes a hole that is easy to miss.** `theme.json`, `text.json`,
`localstorage.json` and a slot overwrite change the rendered document but do
**not** bump `current_version`; and `/render/{id}/` has neither an ETag nor a
cache (`no-store`), so a conditional request cannot catch the edit. What is
returned is the same mtime tuple the server uses to key its render cache.

## State as of 12.10.2026: what is written, what is wired, what is verified

The code for phases 3–5 lives in `agent/mirror/` (`layout`, `store`, `sync`,
`serve`, `schedule`, `assets`, `libui`, `web/`), the window and folders are next
to it in `agent/` (`appwindow.py`, `folders.py`), the server half of paths is in
`app/services/observations.py` and migration `0009`; all of it is described in
`agent/README.md`. This section records the decisions of three passes and the
honest boundary of what has been verified. The boundary is short and worth
reading before the lists: everything has been verified only on macOS, there was
no real OnePage server in any pass, nobody has ever looked at the pages in a
browser, and after the 12.10 pass **the kit does not build into a working file**
(see the last subsection).

### Two prefixes on loopback: the shell on `/library/`, serving on `/mirror/`

The library pages (`web/library.html`, `viewer.html`, their JS and CSS,
`strings.json`, `data.json`, key intake and "Update now") live at
`/library/...` (`agent/mirror/libui.py`); the report itself, its slots and assets
— at `/mirror/<id>/...` (`agent/mirror/serve.py`). These are not two naming
styles but **two different trust boundaries**. Serving knows nothing about the
device key and does not touch the network; the shell accepts the key and
triggers sync. The code that holds the secret is gathered in one file, and the
claim "the key never reaches the log or `data.json`" is verified by reading one
module rather than the whole package. A second consequence:
`layout.parse_local_path` matches report addresses against `/mirror`, and a
`/library/...` address is not a report to it — the list of copies can never be
confused with the contents of a copy. Both sets of addresses are enabled in
`AgentHandler` right after the `Host` check (anti-DNS-rebinding), so that check
applies to them too. POSTs to `/library/*` additionally require the agent's own
Origin (a foreign one is 403, an empty one is allowed as a non-browser client:
the same logic as the platform's `OriginGuard`). The shell sends no CORS headers:
echoing ACAO for the platform's origin would give its page the right to read the
list of reports from someone else's machine. The file name in the address is
matched against an allowlist and never turned into a path; there is deliberately
no percent-decoding — `%2e%2e` never occurs in an honest address, so it is a 404,
not normalization.

### Kit resources live in `mirror/` next to `op_agent.py`

`deploy/build_agent_single.py` glues the modules into one file and **carries over
only code**. `strings.json` and `web/*` stay files, so in the kit they live in a
`mirror/` subdirectory next to `op_agent.py`, and `agent/mirror/assets.py` looks
for a resource under three roots (next to the module, in `mirror/` next to the
module, in `mirror/` next to `sys.argv[0]`) and returns `None`, not an exception,
when it finds nothing. The builder places the resources itself
(`--install-resources`), and before packaging `make-agent-kit.sh` runs
`--check-kit`: a byte-for-byte comparison with the repository (which also catches
the CRLF re-encoding that `perl` applies to `.bat`). Next to the generated
`docs/agent_m2_kit/op_agent.py` the resources are deliberately not laid out: a
copy in the repository would one day silently diverge from the original.

Gluing a subpackage also brought something new: relative imports are rewritten
by parsing, not with a regular expression; global name collisions between
modules (`URL_PREFIX` in `layout` versus `libui`, `CHUNK` in `store` versus
`serve`) the build **renames** (`URL_PREFIX__libui`) rather than failing.

Review found a hole in this mechanism: if a namesake appears in a module glued
EARLIER than `layout`, the dropped `from .layout import atomic_write` would
silently bind to someone else's function with a green build. **The hole is
closed**: references in the importing module are now rewritten to the new name
(`_plan_import_renames`), and anything renaming cannot resolve fails the build
instead of building silently. On top of that, `libui.py` names its prefix
`LIBRARY_PREFIX`, so there is simply no `URL_PREFIX`/`_json` collision with
`layout` and `serve`. The gluing still has no tests of its own — it is verified
only by `--check-kit` and by the fact that the built file imports.

### Serving was not wired, and how that was caught

Before this pass `serve.serve(handler)` was not called from anywhere:
`smoke_mirror.py` brought up its own `ThreadingHTTPServer` and called serving
directly, so 89 checks were green for a mirror that no browser could open. Now
`agent/tests/smoke_mirror_wiring.py` goes through the **real `AgentServer`** —
the same class and handler as in `__main__` — and walks the contract's
addresses; every protective check (23 path-traversal variants, key leakage, 421
on a foreign Host, the RLS boundary) has a positive control, otherwise it would
pass vacuously against a missing route. When the test was written it gave 4/69 —
exactly the hole it was meant to catch; after wiring — 69/69, and after the
second pass 99/99 (checks were added for the honesty of the `POST /library/sync`
response and for the no-schedule state). Still, nobody has opened these pages
in a browser: the test checks response codes, headers and bytes.

### What works, what still stands and what is unverified — before handing out the kit

Every item was verified by reading code, by import, or by a live request on
macOS on 11.10.2026. What was fixed is rewritten as "before/after", not struck
out: the previous revision of this section was read, and a reader must be able
to see what changed.

**Closed over two passes.**

1. **The schedule starts, and the mirror syncs for the first time.** Before:
   `schedule.MirrorWorker` called `sync.sync_all(server, key, progress=, stop=)`
   at module level, and `sync.py` had NO such function — nor the words
   `progress`/`stop`; `enabled` = False, `start()` → False, and the log said
   "module agent.mirror.sync not found" (the message was wrong: the module
   existed). That is, the mirror had never updated even once, with 89 green
   checks. After: `sync.sync_all` exists at the bottom of `sync.py` with exactly
   the signature the `schedule.py` docstring named; `enabled` is true (verified
   by import), the `op-mirror-sync` thread comes up, `progress(done, total)`
   reaches the icon's tooltip, and `stop` is consulted not only between reports
   but before every chunk of the response body (`_StoppableReader` →
   `_stop_point`, including inside `layout.atomic_write`): "Exit" does not wait
   for a multi-megabyte slot to finish and does not leave a stub committed as a
   whole version.
2. **The mirror does not depend on the icon.** Before: the controller was created
   ONLY in the branch with the icon, and `MirrorWorker` brought up the tray after
   itself — so `--no-tray`, lack of support, or a failed window creation left the
   person with a library whose copies would never update, and without a single
   error: `data.json` answered `idle`, the "Update now" button silently did
   nothing. After: `__main__._serve` creates the controller and calls
   `start_mirror()` BEFORE the icon; a repeated start on a live thread is a
   no-op. Verified by a live run of `python -m agent --no-tray` on macOS:
   `/library/` answers 200, `data.json` carries `sync.enabled: true`,
   `POST /library/sync` — `{"started": true}`.
3. **Failure no longer looks like success.** Before: `Syncer.sync_all()` wrote
   `manifest_error` and returned a result, `_run_once` read a normal return as
   "everything is fresh" and moved `last_ok` — a lost connection was shown to the
   person as "Copies updated". After: the module-level `sync_all()` raises an
   exception (`_failure`), and the exception class is the only thing `classify`
   reads; `Syncer.sync_all()` keeps its "fields, not exceptions" contract, and
   two contracts do not live in one method.
4. **`POST /library/sync` and `sync_now()` answer honestly.** `False` when there
   is no schedule, the thread is dead, or a pass is already running; on the page
   — 503 `no_schedule` or 200 `already_running` with a reason, rather than the
   former empty `{"started": false}`, after which the person pressed the button
   again.
5. **Quota and disk are different states with different texts.** Before: ENOSPC
   and our own ceiling landed in one field, `M_NO_DISK` was never set at all, and
   the text advised "free up space" — wrong advice when the quota is hit. After:
   `disk_full` versus `full`, `kind="disk"` versus `kind="quota"`, the kinds
   `M_NO_DISK` and `M_QUOTA`, the texts `sync_no_disk_space` and
   `sync_mirror_full` (the second says nothing about the disk — it may be empty),
   statuses `no_disk_space`/`mirror_full` in `data.json`, both drawn in
   `library.js`. The ceiling (4 GB, `sync.DEFAULT_QUOTA_BYTES`) is still not
   configurable, so "ask to raise the limit" means a code change.
6. **Dates and the cost of `data.json`.** `fetched_at`/`last_sync_at` are not sent
   as `null` (the page printed "31.12.1969"), and the check sits on both sides;
   and the mirror is enumerated with one top-level walk instead of two
   `store.list_mirrored()` calls with a recursive size count for a field that is
   not in the response.
7. **An empty library names itself.** Before: a kit without `mirror/strings.json`
   opened as a page without a single label and stayed silent, and `setup.bat` did
   not diagnose it. After: `--check-kit` at build time, `op_agent.py check-kit`
   from `setup.bat`, the agent's own check at startup (a refusal in `MessageBoxW`,
   because there is no console when launched from autostart) and a banner listing
   the missing files, which `libui` inserts straight into the HTML.

**Still stands, verified by code.**

8. **Slot encoding is read from the NAME, not from `enc`.** `sync` writes
   `record["enc"]` from the response's `Content-Encoding` and in decision 1 of its
   docstring calls this the source of truth for serving, while
   `serve._target_from_record` takes the encoding from the original name
   (`disk_encoding(name)`) and never reads the `enc` field anywhere. A raw slot
   (`data.bin`) that the server delivered compressed will land on disk as brotli
   but be served as `identity` — the browser gets br disguised as text. The
   smokes do not catch this: in them the name always carries `.gz`/`.br`, i.e.
   it matches `enc`.
9. **The 403 class diverges.** `sync._open` raises `TransportError` on 403 →
   `classify` → "server unavailable" and fallback; `classify` itself treats 403
   as a key error, but an `HTTPError` from the syncer never reaches it. Two
   opinions about one response code.
10. **The key is checked only for form** (a non-empty string of printable ASCII
    without spaces, ≤512). Authenticity is not: "expired" and "not accepted" are
    distinguished by `schedule` via its own `key_accepted` flag, which `Syncer`
    knows nothing about.
11. **There are no local runs** (`layout.add_local_run`, `can_update_locally`,
    `next_local_n` are called nowhere — only `rights_expired`, the rights banner,
    works), and `state: fresh|outdated` is not returned in `data.json`: offline
    there is nobody to ask, and `state.json` does not store a "the platform has a
    newer one" flag. The labels "Up to date"/"The platform has a newer version"
    will not appear until the syncer starts writing it.
12. **`strings.json` has no `action_open_library` and `action_show_state`** — they
    come from `_MENU_FALLBACK` in `schedule.py` (verified by reading the file).
13. **The secret store is DPAPI, not Credential Manager.** `agent/README.md`
    promised Credential Manager (M3); `agent/creds.py` does `CryptProtectData`
    through ctypes. The README was fixed on 11.10.2026 — but DPAPI itself has
    never been executed (see item 14).

**Unverified, and we must not pretend otherwise.**

14. **Everything was verified only on macOS.** No real Windows 11 was involved
    for the tray icon, the DPAPI key store (outside Windows without
    `OP_AGENT_DEV_CRED_STORE=1`, `creds.py` raises `CredError`), autostart via
    `pythonw.exe`, or the kit. **There was no real OnePage server in any pass**:
    everything the syncer knows, it learned from the platform stub in the smokes.
    **Nobody has ever looked at the library and viewer pages in a browser** —
    they were checked by response codes and bytes.
15. **A report with an external snapshot.** The code that downloads `snapshot.js`
    exists and runs — against the stub (`snapshot_external: true`, downloaded,
    served as `text/javascript`). What has not happened: a real report with
    `OP_SNAPSHOT_EXTERNAL` and proof that it opens NOT empty — without the
    snapshot the document is empty, and only the eye can see that.
16. **A compressed slot.** The `Content-Encoding` branch has run (gzip straight
    through, decompression for a client without gzip, `gzip;q=0`, `.br` straight
    through, an honest refusal to a client without br) — but against the stub,
    with a fake instead of real brotli bytes (`b"BRBLOB"`) and with names that
    give the encoding away. Neither real brotli nor the case from item 8 has
    happened.
17. **A second pass with 304.** The conditional request, zero traffic,
    `unchanged`, and an unconditional retry when a file has gone missing from
    disk are pinned — against the stub, which issues the ETag itself. Against
    the real `serve_slot_download` with its ETag `"{fp}-{rep}"` this has never
    happened.
18. **Scale and disk.** A mirror of dozens of reports and gigabytes has not been
    assembled; the ceiling has been verified only by refusal BEFORE download
    based on the promised size (a 5 GB slot with a 4 GB ceiling); there has been
    no real ENOSPC on a full disk.
19. **A residual risk you need to know before handing out the kit.** If icon
    creation fails on a person's machine, the mirror PREVIOUSLY did not start at
    all — item 2 closes exactly that. But the fix can be verified only where an
    icon exists: on macOS the path WITHOUT an icon was verified (by a live run),
    on Windows — not at all. Until that happens, item 2 is closed on macOS and
    not closed on Windows.

### The 12.10.2026 pass: window, folders, paths — what of the contract was carried out

The contract in the "Window and folders" section below was written IN ADVANCE so
that three pieces could be built in parallel. They were built in parallel, without
seeing each other's files; here is what fit, what did not, and where the contract
had to be interpreted. Item-by-item details are in `agent/README.md`,
"Discrepancies", items 26–37.

**Carried out literally.** `agent/appwindow.py`: `find_edge()` and
`open(url, *, state_root, title) -> "app" | "browser"`, never raises;
`controller.open_library()` calls only it, `webbrowser` remains a fallback path
inside it, and the chosen path is written to the log. The shortcut is
`setup.bat` `[7/7]`, the icon is `tools/make_app_icon.py` (stdlib, deterministic).
`agent/folders.py`: `load/Source/Entry/resolve/Resolved/ResolvedFile/Missing/identity`
with the same names and the four `Missing` reasons; the masks are a port of
`globmatch.py`; "a missing folder speaks up" is pinned — an empty `files`
without a non-empty `missing` never happens. Platform: `files[].path` and
`roots[{dir, root_path, root_id}]` in `POST …/observations`, the columns
`source_files.path`, `data_sources.root_path/root_id` (migration `0009`,
additive only), display on `/d/{id}/data-sources`, in provenance and in the
lineage drawer, the right is unchanged, the path reaches neither the prompt nor
a share link (pinned).

**Where the contract was silent.** The RFC did not say where `path`/`roots` go in
the observations protocol — the platform implementer accepted them both in the
start manifest and in the batch (the batch wins); when the application starts
sending, it must send exactly that way. `include`/`exclude` are section keys, not
entries, and do not apply to a named `file`. A `file` that points at a directory
returns `not_a_dir` — there is no fifth reason, and one was deliberately not
introduced. UNC is "an ordinary path" per the contract, but is normalized in a
separate branch: `os.path.abspath` on POSIX would prepend the CWD, and `identity`
would depend on where the program was started. An unknown `errno` → `not_found`,
not `denied`.

**What diverged at the seam — and this blocks handing out the kit.** The contract
name `open` in the single-file `op_agent.py` glue becomes a top-level `def open`
(`op_agent.py:2230`) and shadows the built-in `open()` for all modules glued
after it — the mirror store fails on `open(path, encoding=...)`,
`smoke_mirror_kit` is red. `_plan_renames` resolves only module-versus-module
collisions and knows nothing about `builtins`. The contract does not change
here: fix it in the builder (renaming a name that matches a `builtins` name) or
with an alias inside `appwindow.py`. Until it is fixed, the kit compiles and does
not work.

**What was NOT done, plainly.** `folders.py` is wired to nothing — not to the
schedule, not to the window, not to sending paths; only the test calls it. The
caveat "folders are matched by name, not verified" has been lifted for the
browser only halfway: `root_id` is displayed but does not take part in grouping
sources, so two same-named folders on different paths will merge into one source
with the path of the latest run; the source key is the owner's decision, and it
changes the schema. The port and the unsigned `pythonw.exe` remain — the window
is Edge on top of the same loopback server; WebView2 without a port is the next
step. **Windows has still not been tested by anyone**, now including Edge
`--app`, the registry, the shortcut, `casefold`, UNC against a live share, and
ACLs. Of the platform smokes, `generator`, `quality`, `catalog`, `xss`, `share`,
`observ` did not finish; nobody has pressed the "Copy path" button in a browser.
Green on macOS: `smoke_folders` 48, `smoke_appwindow` 30, `smoke_source_paths`
68, `smoke_mirror_sync` 98, `smoke_mirror_wiring` 99.

## What is not done yet

Phases 6–10 of the plan: the R2 runner for local runs, the local-version space
with a catch-up queue, extending the offline kit to pandas/numpy/calamine
(`check_wheels.py` takes the wheel list as a parameter, but pandas/numpy have
not been added to it). And closing what still stands from the list above —
before handing out the kit: slot encoding from `enc`, the 403 class, and
testing on real Windows.

**Two places where trouble is expected, and they must be checked before
implementation, not after.**

First: **determinism**, and here a mistaken argument that stood in this document
in its first revision needs correcting right away. It said: "the divergence is
already measured — `docs/ENGINE2.md`, seven columns out of thirty-two". That is a
false analogy. ENGINE2 compares **two different parsers** (pandas versus Rust
calamine), and each of the seven divergences there is explicitly attributed to a
type-inference step that the second engine simply does not have. It has nothing
to do with the question "the same pandas 3.0.2, built for wasm and natively" and
says nothing about it.

It still has to be measured, but for a different reason and with a different
expected magnitude. The real divergence mechanism here is that **wasm32 is a
32-bit platform**. Hence two concrete suspicions to check:

1. **Default integer width.** numpy's default integer type follows the
   platform's `long`. On a 32-bit platform that is 32 bits; on native x86-64 it
   is not. The sum of a large column can overflow in one environment and not in
   the other, and silently. This is the most likely source of "the numbers
   diverged", and it has to be checked with sums, not with the schema.
2. **Floating point.** Emscripten's libm and possible differences in vector code
   paths produce divergences in the last digits. For a report that prints
   "163 548 bottles" this is most likely irrelevant, but for reconciling against
   the course's reference answer it is not.

So the check is not "do the column types match" but **running the same bundle on
the same folder in the browser and locally and reconciling the output numbers**.
Until that is done, the promise "updated locally, then published, and it is the
same report" is unconfirmed.

Second: **two contracts instead of one**. `agent/generator.py` claims "one
convention for the browser and the agent", but in fact the browser has an R2
session (`begin`/`add_file`/`finish`), while the agent has a CLI `--input-dir`
into which all files are placed at once. For GreenPlum that is natural; for a
folder of Excel files it is not: O(1) memory, the cache and the `service`
disposition live only in R2. The runner will have to be written, not reused.

And third, a practical one: **antivirus**. A no-install folder with an unsigned
`pythonw.exe` listening on a port is the most likely reason this will not work
on a corporate machine. We will not be the ones to find out — the first user
will.

## Window and folders: an application instead of an add-on (12.10.2026)

The owner, 12.10: "we need a proper application on the computer, not a
leech agent", "let the paths go up to the platform, so the team at least won't
get confused — every time it is the same story: one department in one Excel,
another in another, they don't understand which one they need". Both decisions
are made and not to be revisited; below is what they turn into in code and which
seams are declared IN ADVANCE so that three pieces can be written in parallel
and still fit together.

### Why "leech" is true, and where it lives

One line: `agent/controller.py:118` opens `webbrowser.open(
'http://127.0.0.1:<port>/library/')`. That is, the report library is a browser
tab invoked from the menu of the GreenPlum executor's icon. All the other code
(syncer, store, serving, pages) is not tied to the shell: only WHAT the window is
opened with changes.

### Decision 1. The window

* Its own window without browser chrome: `msedge.exe --app=<url>` with
  `--user-data-dir` INSIDE the program folder (we do not touch the person's
  profile) and `--window-size`. Edge is always present on Windows 11, so nothing
  is added to the kit and nothing is downloaded.
* A shortcut "OnePage — my reports" with our icon on the desktop and in the
  Start menu, via HKCU/`WScript.Shell`, without admin rights — like autostart.
* `webbrowser.open` remains the FALLBACK path: Edge not found (a rare Windows
  build, corporate policy) — we open as before and say so in the log, rather
  than crashing.
* Its own window icon = its own `.ico` in the kit. It is drawn by a pure-stdlib
  script (PNG inside ICO), so as not to drag Pillow into the offline wheels.
Window contract (declared in advance, like the folders one):

```
agent/appwindow.py      # stdlib only
  find_edge()           -> Path | None     # msedge.exe: ProgramFiles(x86|)/registry HKCU+HKLM
  open_window(url, *, state_root, title) -> "app" | "browser"
                        # NEVER raises: no Edge or launch failed —
                        # webbrowser.open and return "browser"
```

The name is `open_window`, not `open`: the glue puts modules into one flat
namespace, and `def open(...)` would shadow the built-in `open` for everything
glued after it (the kit failed on `open(path, encoding=...)` in the mirror
store). The builder now refuses such a name.

`controller.open_library()` calls only `appwindow.open_window(...)` and logs
which path was chosen: in an "it opened as a tab" problem report that is the
first question.

* **What this does NOT solve:** under the hood there is still a loopback server,
  i.e. an unsigned `pythonw.exe` listening on a port. The next step (not this
  one) is WebView2 with `SetVirtualHostNameToFolderMapping`, where no port is
  needed at all.

### Decision 2. Folders and files — by path

In the browser a folder is available only as a File System Access API handle:
there is fundamentally no path, the folder is identified by its name plus a
fingerprint of its set of file names, permission requires a user gesture, the
tab ceiling is measured (2.75 GB), and the C.6 border (500 files / 500 MB /
35 MB) exists precisely because it is a browser. The application reads the path
as text — and that removes everything listed.

Contract (declared in advance, change only by editing this section):

```
agent/folders.py        # stdlib only, like the rest of the agent
  load(ini_path)        -> dict[dashboard_id, Source]
  Source(dashboard_id, entries: tuple[Entry, ...])
  Entry(raw, kind: "dir"|"file", include: tuple[str,...], exclude: tuple[str,...])
  resolve(source)       -> Resolved(files: tuple[ResolvedFile,...],
                                    missing: tuple[Missing,...])
  ResolvedFile(path, name, size, mtime_ms, entry_index)
  Missing(raw, reason: "not_found"|"denied"|"not_a_dir"|"no_match")
  identity(path)        -> str   # sha256 of the normalized absolute path
                                 # (casefold on Windows), first 16 hex
```

* The config is `folders.ini` NEXT TO the program (not in the profile: the
  folder is portable and moves as a whole), one section per report, several
  entries per section, an entry is a folder OR a specific file, `include`/`exclude`
  masks with the same semantics as `app/services/globmatch.py` (one convention
  with the browser, otherwise two different answers to the one question "what
  was read").
* UNC (`\\server\share\...`) is an ordinary path, no special branch.
* **A missing folder must speak up.** `Missing` is shown in the window and in
  the state, and the run does not start. A silently successful run over an empty
  location is the worst possible outcome, and it is pinned by a test.

### Decision 3. Paths go up to the platform

The owner's decision. The goal is practical: a person must see WHICH EXACT file
was read, not "a folder with that name". Therefore:

* `POST …/generator/runs/{run_id}/observations`: a file gains `path` (absolute,
  ≤512 characters), a source gains `root_path` and `root_id`
  (`folders.identity`). The browser engine does not send them and is not
  required to: the fields are optional, and an old client works as it did.
* **The shape in which this went to the server (added on 12.10 AFTER
  implementation — the server half chose it itself, and now it is the contract
  for the application):** for a file — `files[].path` (allowed both in the
  start manifest and in the observations batch; the batch wins), for a source —
  a separate list `roots: [{dir, root_path, root_id}]`, where `dir` is the INDEX
  of the folder in `dirs` of the same request. An absent field does NOT erase a
  previously known path; a non-string and a garbage `roots` are silently ignored
  (the field is optional; a 400 on garbage would cost the whole batch). When the
  application starts sending observations, it must send exactly this.
* Storage: `source_files.path`, `data_sources.root_path` / `root_id`
  (migration 0009, adding columns only).
* Display — where sources already live today: `/d/{id}/data-sources`, the
  provenance page, the lineage drawer. The right is THE SAME as now
  (`can_update_dashboard_data`): not a new restriction but an existing one.
* Boundaries that do not move: the path does NOT go into the model prompt (it
  carries only table and column names anyway), does NOT appear on anonymous
  surfaces (share link), and still not a single VALUE from a file goes to the
  server.
* A caveat that has to be lifted in the documentation: "folders are matched by
  name, not verified" is true for the browser and stops being true for the
  application — it has `root_id`.

### What of this is NOT wired (as of 12.10.2026)

`agent/folders.py` is written and verified (48 checks), but nobody calls it yet:
not the schedule, not the window, not observation sending. The server already
accepts and displays paths — the seam exists on one side. This is deliberate:
there is nobody to read folders until the R2 runner (phase 7), and it sits
behind the numbers gate. Until the application starts sending `path`/`roots`,
paths on the sources pages will be empty for every report built by the browser —
and that is correct, not unfinished work.

### Order

The window and folders proceed in parallel (different files). The nightly run
schedule comes AFTER the numbers gate: as soon as a local run becomes the normal
path rather than the exception, the divergence between wasm and native Python
lands in every run, not in a rare offline case.

## Native folder run (19.10.2026)

Phase 7 from "What is not done yet" — the R2 runner — is written and wired, and
the numbers gate it stood behind has been passed with a WARN status. The
generator can now run **natively on the analyst's machine**, reading Excel/CSV
from local folders; previously it ran only in the browser (Pyodide/wasm) or on
the agent from GreenPlum. The server stays in the cloud and stays the
conductor: the page, under the person's session, opens the run, pushes the
bundle to the agent on `127.0.0.1`, listens for progress and publishes the
result. Not a single byte of the analyst's data goes to the server — as before,
only outputs do.

The decisions were made by the owner on 19.10.2026 and are not to be revisited.
They are recorded together with WHY they are the way they are, because each has
an obvious "simpler" alternative that is wrong.

### Decision 1. One contract — R2; the old CLI is legacy

`begin(out_dir, cache_dir, schema_version, progress)` →
`Session.add_file(name, mtime_ms, size, path)` → `Session.finish()` — the
contract from `docs/GENERATOR_AUTHORING.md` §5, the very one the browser
worker carries out. Natively it is carried out by the new `agent/r2host.py`: a
separate process under ordinary CPython, launched with `-I` (the bundle is put on
`sys.path` by the host; nothing is imported from the analyst's folders), the
protocol is line-delimited JSON in both directions (`add_file`/`finish`/`abort`/`ping`
down; `ready`, `progress`, `disposition`, `warning`, `result`, `error` up). The
driver is `agent/generator.py::run_r2_subprocess`; a branch in `agent/runs.py`
chooses it for a bundle without `sources`. Before this pass a bundle without
`sources` got an EMPTY inbox and built the report out of nothing — silently.

The old CLI `python -m <module> --input-dir … --output-dir …
--schema-version …` is declared **legacy**: it is documented nowhere, not
checked in `genmanifest.validate()`, implemented only by slreport, and remains
exactly for the GreenPlum path (`run_generator_subprocess`), where files are put
into the inbox by `gpsource`. The host does not fall back to it: a module without
`begin()` gets `gen_no_r2`, because "silently run it another way" means a
different cache, different warnings and, in the worst case, different numbers.

**A correction to our own documentation.** The header of `agent/generator.py`
said "one convention for the browser and the agent". That was untrue: the agent
ran the legacy CLI, the browser ran R2. The mismatch between documentation and
code cost a separate investigation, and the wording was rewritten both there and
here, not struck out.

Why a separate process rather than importing the generator into the agent:
isolation (a pandas crash ≠ the daemon's death), clean cancellation (`abort` →
pause → `terminate` → `kill`), and stdout: the contract historically allows a
generator to print, and pandas with calamine print on their own, from C code.
Protocol frames therefore go to a duplicate of fd 1, while the real fd 1 is
redirected to stderr (`os.dup2(2, 1)`); the standard `pass_fds` does not exist
on Windows — and Windows is exactly where the kit is going.

### Decision 2. `path` is the analyst's real file; disposition is informational

In the browser the engine puts a copy of the file into the wasm FS: `parsed`/`cached`
are deleted right after `add_file`, `service` lives until `finish()` — this is
memory saving over a copy nobody minds losing. Natively there is no copy; `path`
points at the workbook in the person's folder. The agent deletes, renames and
keeps open NOTHING; disposition becomes purely informational (it goes out as a
frame and into observations). For generator authors this is a new rule: the file
is read-only, do not keep it open after `add_file` — otherwise Windows will not
let the person save the workbook. It is recorded in the authoring guide with
examples of what not to do. In the code, `_add_file` carries a warning for the
next reader: "someone forgot to clean up here, like in the worker" — this is not
forgetfulness; an added `unlink` would wipe the analyst's source data.

### Decision 3. Absolute paths are not baked into the report snapshot

Measured: slreport puts the input folder's path into `dashboard.html`. In the
browser that is a harmless `/inbox`; natively, the real path on the person's
disk ends up there, and everyone who opens the report will see it, including via
an anonymous share link. The authoritative place for paths is provenance and the
sources page (`source_files.path`, `data_sources.root_path`, `root_id`,
"Decision 3. Paths go up to the platform" above): they sit behind the
`can_update_dashboard_data` right, readers and share links do not see them, and
they do not reach the model prompt. The rule for authors: nothing from `path`
goes into `dashboard.html`, slots or `write_back`; the report knows only the
names of its inputs. This is a guide rule, not an engine check: the parity gate
(decision 7) shows such a path as textual drift but does not fail.

### Decision 4. The input set is not frozen

The browser read a snapshot of the folder through File System Access; the agent
reads a live disk: OneDrive syncs, the person saves a workbook mid-run. What the
folder walk measured was, on a long run, minutes ago, and the cache key is
computed from `mtime+size` — a stale pair = a cache entry with someone else's
content. So `r2host._stat_file` re-stats `mtime_ms`/`size` right before
`add_file` and gives the generator the fresh values; a mismatch with what was
sent is an `input_changed` warning, a change to the file DURING `add_file` is an
`input_modified` warning. The run does not stop: stopping it would make OneDrive
the cause of failure for every other nightly run.

### Decision 5. The cache can get poisoned — the `RUNNING` marker

A run killed halfway (Ctrl-C, OOM, a yanked laptop) leaves a half-written cache
entry; its key will match, the generator will read it as `cached` and SILENTLY
produce a wrong report — the worst outcome, because there is nothing to notice.
`r2host._arm_cache` places a `.r2host-running` marker in `cache_dir` before
`begin()` and removes it ONLY after a successful `finish()`; a broken pipe before
`finish()` leaves the marker. A marker found at startup → the entire cache
directory is wiped (which entry is half-written cannot be seen from outside —
there is nothing to repair selectively), the run gets a `cache_reset` warning and
runs cold. The price is one cold run after a crash; the alternative is a wrong
report with no sign of being wrong.

### Decision 6. Scenarios in `folders.ini`

`[folders:<dashboard_id>]` is the default set, `[folders:<dashboard_id>:<name>]`
is a named scenario. It is needed for scenario planning ("the same data, but
without Irkutsk", "plan B"): one report, several sets of input files, comparison
through the platform's ordinary versions. A colon as the separator is safe: a
report id is a UUID. The code lays down the key (`Source.scenario`,
`Source.key`, `folders.scenarios_for`), selection by `meta.scenario` in
`runs._resolve_folder_files` (an unknown scenario is refused with
`folders_no_scenario` listing the known ones) and `GET /folders?dashboard=…&scenario=…`
on the agent (contract K1: the response carries `scenario` and a `scenarios`
list), and the page puts `scenario` into the `input_manifest` of a `local` run
(contract K3, `buildLocalInputManifest`) — without that the run could not say
which set it was built from, and there would be nothing to compare. **There is
no selection UI yet** — the key and the field are laid down; choosing a scenario
on the page is separate work and is not part of this document until it is merged
and verified by a live run.

### Decision 7. The numbers gate is passed with a WARN status

The tool `tools/engine_parity.py` plus `tools/run_bundle_pyodide.mjs` run one
bundle on one folder twice — in Pyodide under node and natively through the same
`r2host.py` — and compare the outputs **in layers**: bytes (the only PASS), then
decoding and numbers (WARN if no number moved beyond a 1e-12 relative difference
and the dispositions matched), an opaque format without `--decoder` is a FAIL.
The self-test of the comparison layer is `tests/smoke_engine_parity.py`.

The result on slreport 2.2.25 and 36 synthetic workbooks: the slot is
byte-for-byte equal, 3527 numeric literals in the html matched, only the
timestamp and the input folder path (decision 3) diverged. At the same time the
probe confirms that the divergence mechanism IS REAL: `np.array([1]).dtype` is
`int32` in wasm versus `int64` natively, `sys.maxsize` is 2147483647 versus
9223372036854775807, the sum 8×2³⁰ is 8589934592 natively and 0 in wasm. pandas
holds `int64` explicitly, so its path is safe; the risk remains wherever a
generator calls bare numpy without a `dtype`.

**The conclusion, stated plainly:** the gate proves parity of ONE generator on
ONE data set. It has to be run on every bundle that will go into a native run;
slreport's parity says nothing about the next generator.

### What is NOT verified — and must not be declared done

* **Windows has not been seen at all.** The whole native run, `r2host.py`, the
  fd 1 duplicate, locks on open workbooks, OneDrive paths with spaces and
  Cyrillic, `casefold` in `identity` — verified only on macOS. The rule "do not
  keep the workbook open" is derived from how Windows works, not from a run on
  it.
* **Parity is on one generator and one set.** slreport 2.2.25, synthetic data;
  the gate has not been run on real data or on other generators. The bare-numpy
  without `dtype` branch has not been looked for in the generators themselves.
* **Write-back into the analyst's folder has not been tested.** `write_back`
  natively reaches the agent's `out_dir` and is served to the page; what the page
  or the agent does with it next (writing into the person's folder, the way the
  browser writes into write-flagged folders) is not implemented and has not been
  checked.
* **The opaque slot is reconciled only by bytes.** `sl_bc_data.bin.gz` is
  byte-for-byte equal — and that is a good outcome, but at the first byte
  divergence there is no numbers layer for it: a `--decoder` for the slreport
  format is needed, and there is none.
* **Scenarios without a UI.** The key and the field exist, the choice does not
  (decision 6); a run by scenario has been checked only by the `folders.py`
  smokes, not by the page.
* **Paths to the platform from a native run — the seam is built on both sides
  but not verified by a live run.** The agent returns `files[].path` and `roots`
  (`GET /folders`, K1), the page puts them into `input_manifest` (K3), the server
  accepts them (decision 3 of the "Window and folders" section); nobody has seen a
  real path appear on the sources page of a real server.
* **The nightly run schedule** is still not enabled — the order is unchanged:
  AFTER the numbers gate, and the gate so far has been passed for one bundle.

## Offline run and the catch-up queue (19.10.2026, phase 7B)

Phase 7 delivered runs from local folders, but such a run was possible exactly
when a OnePage page was alive: it takes the run lock, keeps the heartbeat, pushes
the bundle to the agent and publishes the outputs. With the server down there is
no page at all — so a person with the right to update data could not update it
precisely on the day this program was written for. The owner's decisions (11.10,
not to be revisited): "a local run must work without the server too" and "when
the server is back — ASK the person, do not publish silently".

Done: `agent/mirror/localrun.py`, an "Update from folders" button in the library
(`POST /library/local-run`), bundle caching during sync, three agent routes for
catch-up, and live `fresh`/`outdated`/`local_only` labels. Pinned by
`agent/tests/smoke_localrun.py` (78 checks, the end-to-end ones going through the
real `AgentServer`).

**The `local/L<k>` space had been written in advance and wired to nothing.**
`layout.py` held the paths, numbering, `add_local_run`, `can_update_locally`,
`rights_expired`, URL parsing and the `local_runs` field in `state.json`;
`serve.py` read an optional `run["slots"]`; `web/library.js` and `strings.json`
carried dead labels. Not a single line of code wrote there. This pass filled in
the scaffold rather than building a second one.

**The bundle is cached during sync — otherwise there is nothing to compute with
offline.** `sync._cache_bundle` stores `<mirror>/<id>/bundle/<sha256>.zip` for
reports the person has the right to update data for, verifies the checksum AFTER
writing (a cache where something else lies under a checksum's name lies to
everyone who looks into it), and sweeps away bundles of past versions. The
directory is separate from `server/v<n>` on purpose: `prune_versions` sweeps
report versions and must not carry off the only thing the report can be
recomputed with.

**The data-delivery bridge is borrowed from the platform, not copied into the
agent.** The generator returns bare html. An interactive report holds no data
inside: it is delivered by `op_ready`→`op_file`, and the script itself is inserted
by the SERVER at serve time (`detector.inject_bootstrap`). A naive "store the
html as is" would give an empty report — a quiet failure instead of a working
one. So the local document gets the same block, cut out of the already-downloaded
`server/v<n>/dashboard.html`, with the slot inventory substituted; the kind of
bridge is recorded in the run as the `bridge` field (`own` / `borrowed` /
`none`). The agent keeps no copy of the bridge of its own: a copy that has
diverged from the original will one day deliver data into the wrong
`input[type=file]`, and that can only be noticed from wrong numbers. The block
marker is checked against `app/services/detector.py` by a smoke — the kit has no
such check and cannot have one.

**The right is limited in time, and that is the only protection.** A right
revoked on the platform arrives only with the next sync. An expired
`rights.expires_at` STOPS the run (`localrun.precheck` → refusal with the date)
and does NOT prevent reading — §5 of this RFC. An RLS report is rejected by three
lines of defense: it is not in the mirror, `precheck` answers `rls`, and serving
is closed (ADR-18).

**Catch-up is a question to the person, and only after the platform is back.**
The "publish to the platform?" callout appears when sync has SUCCEEDED AGAIN
later than the local run; until then the card shows a calm line "numbers saved".
No new route was added on the SERVER: the report page creates an ordinary run
`POST /api/dashboards/{id}/generator/runs` with `engine: "local"` and the SAVED
`input_manifest` (it lies next to the outputs — `local/L<k>/input_manifest.json`,
together with the `observe.json` frames), fetches the outputs from the agent over
loopback (`GET /local-runs`, `GET /local-runs/output`) and publishes with the
existing `publish`. We pay exactly one thing: `started_at` will be the catch-up
time, not the time of the actual run — so the version note carries BOTH dates
("built offline …, published …", `localrun.publish_note`), not one. The
"published" mark is set by the page after a successful publish
(`POST /local-runs/published`): marking it earlier would mean losing a run that
never reached the platform.

**What is NOT done, and named.** There is no offline write-back into folders: the
names of such outputs are written into the run as the `write_back_skipped` field,
and files are not placed into the person's folders (the program must not write
into other people's folders without an owner decision). There is no
`folders.ini` scenario selector in the library — the route accepts the field. The
page side (`static/js/gen-engine.js`) is untouched: catch-up on it is done by the
integration stage, and until then the queue is visible only in the library.
Windows has not been tested — like everything else in this document.
