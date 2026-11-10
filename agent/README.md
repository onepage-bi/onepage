# op-agent — localhost agent #2 (the GreenPlum engine)

A local daemon on the analyst's machine. It executes **frozen SQL** from the
generator's immutable bundle under the **analyst's personal read-only GP
credential**, runs the generator natively and hands the results to the OnePage
conductor page. It listens only on `127.0.0.1` and is unreachable from the
network.
Design and gate log — `docs/RFC_AGENT_GREENPLUM.md`; plan — option c2 from
`docs/RFC_DWH_RESEARCH.md`.

**Important correction (11.10.2026).** This used to say "holds no OnePage
secrets and never talks to the OnePage server". That stopped being true once the
report-mirror role arrived (`docs/RFC_DESKTOP_MIRROR.md`): the agent now makes
outbound requests to one platform — our own — and holds an access key for it.
The line was rewritten rather than deleted, because a stated security property
must not be withdrawn silently.

What did NOT change and remains an invariant: the agent still listens only on
loopback and accepts nothing from the network, and the key it holds **cannot
write** by construction — the check in `app/auth/deps.py` refuses it on any
method other than GET and HEAD, so it can neither modify a report nor issue a
second key.

## Status: M1 (skeleton)

Implemented and covered by a smoke test (`agent/tests/smoke_agent.py`, 16/16):
the whole page↔agent transport — health, pairing (out-of-band code → Bearer),
Bearer authorization, a strict origin allowlist (CORS/PNA), multipart bundle
upload with a `sha256(bundle)==checksum` check (byte-exact against the binary),
safe unpacking (zip-slip guard), an NDJSON event stream (chunked, with replay
for late subscribers), output download, cancellation, single-instance locking
over the port ladder `47113→47114→47115`.

M1 in echo mode: instead of real GP/generator work an echo task runs (it emits
progress events and returns `echo.json` with the list of bundle files) — enough
for the owner to **measure NDJSON latency** on the corporate Edge 149.

**NOT implemented yet (next stages):** the GP executor `COPY→gzip` (`gpsource`,
M2), native generator launch as a subprocess (M2), the tkinter folder dialog +
exe/portable-python packaging (M3). Secrets were planned to live in the Windows
Credential Manager; they are implemented differently — as a DPAPI blob via
ctypes (`agent/creds.py`), and this is not a stopgap: see "Security" below.

## Running (dev / M1 measurement)

From the root of the OnePage repository:

```
python -m agent
```

Listens on `http://127.0.0.1:47113`. State — `~/.op-agent` (Windows:
`%LOCALAPPDATA%\op-agent`). Stop — Ctrl+C.

Test:

```
python -m agent.tests.smoke_agent
```

## HTTP contract (protocol 1)

| Method / path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | `{app,version,protocol,paired,busy,agent_id}` |
| `POST /pair/request` | none | generate a code, print it to the agent console |
| `POST /pair` `{code}` | none | exchange the code for a Bearer token |
| `POST /runs` (multipart `bundle`+`meta`) | Bearer | start a run → `{agent_run_id}` |
| `GET /runs/{id}/events` | Bearer | chunked NDJSON event stream |
| `GET /runs/{id}/outputs/{name}` | Bearer | fetch an output (bytes) |
| `DELETE /runs/{id}` | Bearer | cancel |

Everything except `/health` and `/pair*` requires `Authorization: Bearer <token>`.
CORS: `Access-Control-Allow-Origin` is echoed ONLY for an origin on the allowlist
(`agent.ini`); the preflight carries `Access-Control-Allow-Private-Network: true`.

## Security (in brief)

- The GP password **never crosses the agent process boundary** and is not kept
  in the config: it is a DPAPI blob under the Windows user account (`creds.py`,
  `CryptProtectData`), not the Credential Manager as promised earlier — this has
  not been run on Windows yet; all verification was on macOS with the dev store.
  Even an XSS on OnePage does not steal the credential — it only gets the final
  slot, which is published anyway.
- The pairing code is printed to the agent console (the channel of the person
  sitting at the machine); the token is 256 bits, compared in constant time;
  revocation = restarting the agent.
- Bind only to `127.0.0.1` (unreachable from the LAN); auth is Bearer, not a
  cookie.

Config — `agent.ini` (template `agent.ini.example`); do not store the password
in it.

---

## Desktop report mirror (`agent/mirror/`)

What and why — `docs/RFC_DESKTOP_MIRROR.md`. The mirror keeps copies of
published reports on the person's machine so they can be read while the
platform is unavailable. It is **not** a backup: that is the job of
`deploy/backup.sh` and `deploy/restore-drill.sh`. The mirror answers a different
question — how to read a report right now, without the platform. Standard
library only, HTTP via `urllib.request`.

**Status as of 11.10.2026 (after two passes): the mirror is able to sync for the
first time.** The entry point `sync.sync_all(server_url, device_key,
progress=, stop=)` exists at module level, the schedule finds it and starts the
thread, serving and the library are wired into `server.py`, and the mirror is
now started not by the tray icon but by `__main__._serve` — before the icon and
independently of it.

What this rests on and what it does NOT. Verified on macOS: `python -m agent
--no-tray` answers 200 on `/library/`, `data.json` carries `sync.enabled: true`,
`POST /library/sync` — `{"started": true}`; the smoke tests are green
(`smoke_mirror` 89, `smoke_mirror_sync` 95, `smoke_mirror_wiring` 99,
`smoke_tray` 54). **Nothing that only happens on a real user's machine has been
verified:** nobody had a real Windows 11, real DPAPI, a real tray icon, real
autostart or a real OnePage server, and the library and viewer pages have never
been looked at in a browser — they were checked by response codes and bytes.
The remaining discrepancies are in the "Known discrepancies" section below;
until it is empty, it is too early to say "the mirror works".

**Pass of 12.10.2026: an application, not an appendage.** The owner called the
program "a leech agent", and the reason sat in one line — `controller.py` opened
the library with `webbrowser.open`, as one tab among others. Now the library
opens in its own window (`appwindow.py`, Edge `--app`) and from a shortcut,
folders are specified as paths in `folders.ini` (`folders.py`), and file paths
travel to the platform and are visible on the data-source pages. The contract
for these three pieces was declared in advance in the RFC ("Window and folders")
and carried out literally; what of it works, what is not wired and what is not
verified — "Known discrepancies", items 26–37. Whoever builds the kit should
read item 29 first: **the build is currently red**, and it must not be
distributed.

### Modules

| File | Responsible for |
|---|---|
| `mirror/layout.py` | The layout contract: paths, slot file name (`safe_slot_filename`), the `state.json` schema, local `/mirror/...` addresses, `atomic_write` (`.tmp` alongside, `fsync`, `os.replace`). The "there is never a half-written file" invariant lives here. |
| `mirror/store.py` | Writing to disk via `layout`, space accounting by walking the directory, the ceiling (`MirrorFull`), deleting a report together with recording the reason (`mark_blocked`), sweeping old versions and `*.tmp`. |
| `mirror/sync.py` | `Syncer` — one pass: manifest, version and `fingerprint` comparison, slots with `If-None-Match`, document and snapshot (we ask for gzip and store it decompressed), rights with `expires_at`, deletion of reports with `mirrorable: false`. The schedule's entry point is the module-level `sync_all(server_url, device_key, progress=, stop=)`: it raises on a failed pass, because `schedule.classify` reads the exception class, not the result fields. The only agent module that talks to the OnePage server. |
| `mirror/serve.py` | Local serving of the document, slots and assets at `/mirror/<id>/...` on `127.0.0.1`. `serve(handler)` is called from `server.py` (`do_GET`/`do_POST`/`do_DELETE`) right after `_host_ok()`. |
| `mirror/localrun.py` | **Offline run**: bundle from the mirror cache → `runs._resolve_folder_files` → `generator.run_r2_subprocess` → `local/L<k>` + `input_manifest.json` and `observe.json` alongside. It knows nothing about the network. Rights are checked by `precheck` (RLS, `can_update_data`, the `expires_at` deadline, presence of the bundle) — the library uses the same check to decide whether to draw the button. The data-feeding bridge is TAKEN from the already downloaded server document (`with_bridge`, the `detector.inject_bootstrap` marker); the agent keeps no copy of the bridge of its own. The catch-up queue — `list_unpublished` / `output_path` / `mark_published`; the OnePage page does the publishing, the program only hands over files and input. |
| `mirror/libui.py` | The library shell at `/library/...`: pages from `web/`, `strings.json`, `data.json` (state in one response), `POST /library/sync` and `POST /library/key`. A whitelist of names, no percent-decoding; POST requires its own loopback Origin; no CORS is sent; the key never appears in a response, the log, or `data.json`. A prefix separate from `/mirror/` — a different trust boundary: serving knows nothing about the key. |
| `mirror/assets.py` | `res_path(name, web=False)` / `strings_path()`: where `strings.json` and `web/*` live — next to the module (package) or in `mirror/` next to `op_agent.py` (the single-file build carries only code). A name is not a path: `../x` gives `None`. |
| `mirror/schedule.py` | `MirrorWorker`: the `op-mirror-sync` thread, backoff (from 1 min to 1 h), failure classification, the status line for the tray icon, `DEFAULT_TTL_DAYS = 90`, `library_url(port)` → `/library/`. |
| `mirror/strings.json`, `strings_README.md` | All of the program's texts. Edited without reading Python. The "which string is shown where" table is in `strings_README.md`. |
| `mirror/web/` | The library (`library.*`) and the viewer (`viewer.*`), static files. Served by `libui.py` only at contract addresses: `library.html`/`viewer.html` are not served under their own names. |
| `../appwindow.py` | The library window: `find_edge()` (`ProgramFiles`, `ProgramFiles(x86)`, `LOCALAPPDATA`, then the App Paths registry HKCU+HKLM) and `open(url, *, state_root, title)` → `"app"` or `"browser"`. NEVER raises: it is called from a menu handler, where an exception would silently kill the shell thread. The Edge profile is `<state>/window`, not the person's profile. The name `open` shadows the builtin, so the module does not open files at all — and it is exactly this that breaks the kit's flat concatenation (item 29). The window icon is `deploy/agent_tools/onepage.ico` from `tools/make_app_icon.py` (stdlib, deterministic). |
| `../folders.py` | Folders and files by PATH: `load(folders.ini)` → `{dashboard_id: Source}`, `resolve(source)` → `Resolved(files, missing)`, `identity(path)`. Masks are a line-by-line port of `app/services/globmatch.py`, not `fnmatch` (live character classes, `*` across `/`, case sensitivity per OS — three different answers to "what was read"). Repeated `dir`/`file` keys go through `_MultiDict`: the stock `configparser` silently overwrites a second key of the same name. The invariant "an empty `files` only with a non-empty `missing`" is pinned by a test. Wired since 19.10.2026: it is called by `runs._resolve_folder_files` (the R2 run) and `server.folders_payload` (`GET /folders`, contract K1) — items 30, 43. A `[folders:<id>:<name>]` section is a scenario (item 45). Example — `deploy/agent_tools/folders.ini.example`. |
| `tray.py`, `controller.py` | The mirror menu items ("Update now", "Open library", "Status", "Add key…") and the status line in the tray icon tooltip. "Open library" goes through `appwindow.open_window`; there is no `webbrowser` in the controller. |

The device key lives in `creds.CredStore` under the name `onepage-device-key`.
This literal appears twice: `sync.DEVICE_KEY_SLOT` and `schedule.KEY_NAME`.
Change them only together. On Windows, storage goes through DPAPI. On other
systems, without `OP_AGENT_DEV_CRED_STORE=1`, accessing the key raises
`CredError`. The key is sent as an `Authorization: Bearer` header.
`Syncer._scrub` strips it from any text that ends up in the log or in an
exception.

### What lives where on disk

The root is `state_dir()`: `%LOCALAPPDATA%\op-agent` on Windows, `~/.op-agent` in dev.

```
<state>/mirror/blocked.json                      reports without a copy, and the reason (rls, draft)
<state>/mirror/<id>/state.json                   inventory: versions, fingerprint, slots, rights
<state>/mirror/<id>/server/v<n>/dashboard.html   the document, decompressed
<state>/mirror/<id>/server/v<n>/<file>           slots; <file> is disk-safe, name is the original
<state>/mirror-schedule.json                     last_ok and key_accepted (survive a restart)
<state>/creds/onepage-device-key.bin             device key, DPAPI blob
<state>/op-agent.log                             log
```

The directory `mirror/<id>/local/L<k>/` is provided for in `layout.dir_for`, but
there are no local runs yet. The quota is `sync.DEFAULT_QUOTA_BYTES`, 4 GB, not
configurable.

### Sync: automatic and manual

The schedule starts when the program starts (`__main__._serve` →
`controller.start_mirror()`); the tray icon is not needed for this: the first
attempt after 20 s, then once an hour, with backoff from 1 min to 1 h when the
server is unreachable. The key is saved from the page
`http://127.0.0.1:<port>/library/` ("Add key") or from the tray menu item;
"Update now" wakes the same thread and answers honestly — 503 `no_schedule` if
there is no schedule at all, and `already_running` if a pass is already under
way.

**Not a single pass has been run against a real OnePage server** (the stand was
unavailable at the time of the review): everything the syncer knows, it learned
from the platform stub in the smoke tests. A manual pass is invoked from Python;
the key goes into the dev store:

```
OP_AGENT_DEV_CRED_STORE=1 python3 -c "import getpass; from agent.creds import CredStore; CredStore().set_password('onepage-device-key', getpass.getpass('key: '))"
OP_AGENT_DEV_CRED_STORE=1 python3 -c "from agent.mirror.sync import sync_once; print(sync_once(base_url='https://<platform>'))"
```

`sync_once` returns a `SyncResult`: `checked`, `updated`, `unchanged`,
`removed`, `failed`, `bytes_downloaded`, `errors`, `key_rejected`, `full`
(the mirror ceiling), `disk_full` (ENOSPC on disk), `manifest_error`, `stopped`,
`needed_bytes`. Without
`base_url`, the first origin from `agent.ini` (`[security] allowed_origins`) is
used, by default `https://onepage.example.com`. The schedule contract is a
different function, `sync.sync_all(server_url, device_key, progress=, stop=)`:
it raises on a failed pass, because `classify` reads the exception class, not
the result fields.

### Log

`op-agent.log` is written when the agent runs with the tray icon
(`applog.setup`). Sync lines start with `[mirror]`. In `--no-tray` mode and on a
manual call, output goes to the console. Error texts pass through `_scrub`.

### Diagnostics: "the report won't open"

1. Do `<state>/mirror/<id>/state.json` and `server/v<n>/dashboard.html` exist? If not, the pass did not download it. Check the log and `blocked.json`.
2. Is `<id>` in `blocked.json`? Then the reason is `rls` or `draft`. By rule there is no copy; this is not an error.
3. Is the file named in the `file` field of each slot in `state.json` present on disk? If not, the slot was not downloaded. Look for the reason in the log.
4. The address `http://127.0.0.1:<port>/mirror/<id>/` gives 404 with a `reason` in the body (`not_synced`, `no_document`, `slot_file_missing`…) and 403 `rls` for a report with `rights.rls_enabled`. You cannot open a report as a file (`file://`): its slots will not load. This is recorded in RFC §2.
5. The `/library/...` pages answer 404 while `/health` is alive — there is no `mirror/` directory with `strings.json` and `web/*` next to `op_agent.py` (the kit was not copied in full). This no longer passes silently: `setup.bat` calls `op_agent.py check-kit`, the agent itself checks the kit at startup (refusing with a window, because autostart has no console), and `libui` inserts a banner listing the missing files directly into the HTML. None of the three checks has been run on a real Windows machine.

### Known discrepancies (review of 19.10.2026; items 1–42 — passes of 11–14.10, items 43–47 — 19.10)

Every item was verified by reading the code, by an import or by a live request
— not by someone's account. Fixed items are rewritten as "before/after" rather
than struck out: the previous edition of this section has been read, and the
reader must be able to see exactly what changed. Where the code diverges, the
documentation above describes the intended behaviour; this section records
exactly where.

#### Closed over two passes

1. **The schedule starts — and the mirror syncs for the first time.** Before:
   `schedule.MirrorWorker` took `sync.sync_all` at module level and called it as
   `sync_all(server, key, progress=, stop=)`, while `sync.py` had NO such
   function at all — nor the words `progress`/`stop`; `enabled` = `False`,
   `start()` → `False`, and the log said "module agent.mirror.sync not found"
   (the message was wrong: the module existed). In other words, the mirror did
   not update EVEN ONCE while 89 checks were green. After: `sync.sync_all`
   exists at the bottom of `sync.py` with exactly the signature named by the
   `schedule.py` docstring; `enabled` is true (verified by import), and the
   `op-mirror-sync` thread starts.
2. **The mirror menu is visible.** `tray._mirror_on()` still requires `enabled`
   — but it is now true, so "Add key…", "Update now", "Open library" and
   "Status" are in the menu, and the text "Key added. Starting the update" has
   stopped promising something that does not happen:
   `controller.set_device_key` → `key_changed()` → `_wake` to the live thread.
3. **`progress` and `stop` are honoured.** `progress(done, total)` reaches the
   tray tooltip; `stop` is a `threading.Event`, and it is checked not only
   between reports but before every chunk of a response body
   (`_StoppableReader` → `_stop_point`), including inside
   `layout.atomic_write`. "Exit" does not wait for the end of a multi-megabyte
   slot and does not leave a stub committed as a whole version. Stopping is not
   a failure: `SyncStopped`; `last_ok` and the state view are left untouched.
4. **A manifest error no longer looks like success.** Before:
   `Syncer.sync_all()` wrote `manifest_error` and returned the result,
   `_run_once` treated a normal return as `M_FRESH` and advanced `last_ok` — a
   lost connection was shown as "Copies updated". After: the module-level
   `sync_all()` raises (`_failure`), and the exception class is the only thing
   `schedule.classify` reads; `Syncer.sync_all()` keeps its "fields, not
   exceptions" contract, because the tray needs the counters.
5. **Serving and the library are wired in.** `serve.serve` and
   `libui.serve_library` are called from `do_GET`/`do_HEAD`/`do_POST`/`do_DELETE`
   right after `_host_ok()`; pinned by `smoke_mirror_wiring.py` (99/99) through
   a real `AgentServer`.
6. **The controller exists without the tray icon too.** Before:
   `AgentController` was created only in the tray branch, and the mirror was
   started by the tray after its own start, so `--no-tray`, missing tray
   support or a failed window creation left the person with a library whose
   copies would never update, and without a single error — `data.json`
   reported `idle`, and `POST /library/sync` silently answered
   `started: false`. After: `__main__._serve` sets `server.controller` and calls
   `start_mirror()` BEFORE the icon; a repeated start on a live thread is a
   no-op, so the call from the tray is safe. Verified by a live run on macOS
   (see "Status" above); on Windows — not, see item 24.
7. **`sync_now()` does not answer "started" when there is nothing to start
   with.** Before: `True` unconditionally, i.e. `_wake` to a non-existent thread
   and "started" about something that was never started. After: `False` on
   `enabled=False`, on a dead thread and on a pass already under way, and
   `POST /library/sync` answers 503 `no_schedule` or 200 `already_running` —
   with a reason the page knows how to render.
8. **Quota and disk are told apart.** Before: `OSError`/ENOSPC and our own
   ceiling went into one field, `M_NO_DISK` was never set at all, and
   `MirrorFull` stayed in the `full` field, which nobody read; the text advised
   "free up space" and "raise the ceiling" — with the quota hit, the first is
   wrong and the second has nothing to act on. After: `SyncResult.disk_full`
   (ENOSPC/EDQUOT) versus `full` (the ceiling), `kind="disk"` versus
   `kind="quota"`, views `M_NO_DISK` and `M_QUOTA`, texts `sync_no_disk_space`
   and `sync_mirror_full` (the second says nothing about the disk — it may be
   empty), statuses `no_disk_space`/`mirror_full` in `data.json`, both rendered
   in `library.js`. The ceiling (4 GB, `sync.DEFAULT_QUOTA_BYTES`) is still not
   configurable, so "ask for the limit to be raised" in the text means a code
   change, not a setting.
9. **`data.json` does not lie about dates and does not walk the disk twice.**
   `fetched_at` and `last_sync_at` without a value are NOT sent as `null`
   (`new Date(null)` printed "31.12.1969"), and `common.js` does not trust such
   a value either — the belt-and-braces is deliberate. The mirror is listed in
   one top-level walk instead of two `store.list_mirrored()` calls, each of
   which computed `dir_size` recursively for a field the response does not even
   contain.
10. **An empty library names itself.** Before: a kit without
    `mirror/strings.json` opened a page without a single label and stayed
    silent, and `setup.bat` did not diagnose it. After: `--check-kit` at build
    time, `op_agent.py check-kit` from `setup.bat`, the agent's own check at
    startup (`_kit_problems`, refusal in a `MessageBoxW`, because autostart has
    no console) and a banner listing the missing files that `libui` inserts
    directly into the HTML. The `assets.py` docstring ("keys are visible on an
    empty dictionary") remains true for the tray and false for the page — but
    the page is no longer silent.
11. **The kit concatenation knows about dropped symbol imports.** Before: on a
    name collision the later module was renamed, and a namesake in a module
    concatenated EARLIER than `layout` would have bound the dropped `from
    .layout import atomic_write` to someone else's function while the build
    stayed green. After: references in the importing module are rewritten
    (`_plan_import_renames`), and whatever renaming cannot resolve fails the
    build; `libui.py` names its prefix `LIBRARY_PREFIX`, so the
    `URL_PREFIX`/`_json` collision simply does not exist. The concatenation
    still has no tests of its own.
12. **Tests.** `agent/tests/smoke_mirror.py` (89 — serving and the syncer
    against the platform stub), `smoke_mirror_sync.py` (95 — the schedule, the
    `sync_all` contract, stopping, state views), `smoke_mirror_wiring.py` (99 —
    a real `AgentServer`: the address contract, MIME, path-traversal variants,
    key leakage, 421 on a foreign Host, the RLS boundary, honesty of `POST
    /library/sync`), `smoke_tray.py` (54). All green on 11.10.2026 on macOS.
    Server side: `tests/smoke_sync_manifest.py`,
    `tests/smoke_device_token.py`.

#### Still open

13. **The slot encoding is read from the NAME, not from `enc`.** `sync` writes
    `record["enc"]` from the response's `Content-Encoding` and, in decision 1 of
    its docstring, calls it the source of truth for serving;
    `serve._target_from_record` takes the encoding from the original name
    (`disk_encoding(name)`) and does not read the `enc` field anywhere. A raw
    slot (`data.bin`) that the server delivered compressed (the server
    compresses raw files according to `Accept-Encoding` and caches the
    variants) will land on disk as brotli with `enc = "br"`, but will be served
    as `identity` — the browser will receive br disguised as plain text. The
    smoke tests do not catch this: in them the name always carries `.gz`/`.br`,
    i.e. it matches `enc`.
14. **The class of a 403 is inconsistent.** On a 403, `sync._open` raises a
    `TransportError` rather than a key error, so `classify` reports "server
    unavailable" and backs off. `classify` itself treats 403 as a key error
    ("auth") — but an `HTTPError` never reaches it from the syncer. Two
    opinions about one status code.
15. **The key is checked only for its shape.** `controller.set_device_key` and
    `libui` refuse an empty key, one longer than 512, a non-ASCII or
    non-printable key, and whitespace (the check is repeated in `libui`
    deliberately: there may be no controller). Authenticity is still not
    checked: `schedule` tells `sync_key_expired` from `sync_key_rejected` by its
    own `key_accepted` flag, and `Syncer` does not know that flag.
16. **Two keys are missing from `strings.json`.** `action_open_library` and
    `action_show_state` are substituted from `_MENU_FALLBACK` in `schedule.py`
    (verified by reading the file: 38 keys, these two are not among them).
17. **There are no local runs.** `layout.add_local_run`,
    `layout.can_update_locally` and `next_local_n` are not called anywhere; of
    this family only `layout.rights_expired` works — the `rights_blocked`
    banner in `libui.data()`. The `local_only`/`local_at` fields are
    deliberately not served in `data.json`: `library.js::openUrl` builds a link
    only into the server space.
18. **`state: fresh|outdated` is not served.** Offline there is nobody to ask,
    and `state.json` does not store a "newer on the platform" flag; the labels
    "Up to date"/"Newer on the platform" will not appear until the syncer starts
    writing it.
19. **The secret store is DPAPI, not the Credential Manager.** The Credential
    Manager promise (M3) in this README was corrected on 11.10.2026 —
    `agent/creds.py` calls `CryptProtectData` via ctypes. DPAPI itself has never
    been executed: outside Windows, without `OP_AGENT_DEV_CRED_STORE=1`,
    accessing the key raises `CredError`, and all verification was done on the
    dev store.

#### Not verified — and must not be passed off as verified

20. **Everything has been verified only on macOS.** Neither the tray icon, nor
    the DPAPI key store, nor autostart via `pythonw.exe`, nor the kit has ever
    run on a real Windows 11. **No pass ever ran against a real OnePage
    server** — everything the syncer knows, it learned from the platform stub
    in the smoke tests. **The library and viewer pages have never been looked
    at in a browser**: they were checked by response codes, headers and bytes.
21. **A report with an external snapshot.** The code that downloads
    `snapshot.js` exists and runs — against the stub (`snapshot_external:
    true`, downloaded, served as `text/javascript`). What has not happened: a
    real report with `OP_SNAPSHOT_EXTERNAL` and proof that it opens NOT empty.
    Without the snapshot the document opens empty, and that can only be seen
    with one's own eyes.
22. **A compressed slot.** The `Content-Encoding` branch has run (gzip passed
    through, decompression for a client without gzip, `gzip;q=0` honoured,
    `.br` passed through, an honest refusal to a client without br) — but
    against the stub, with a fake in place of real brotli bytes (`b"BRBLOB"`)
    and with names that give the encoding away. Neither real brotli nor the
    case from item 13 has occurred.
23. **A second pass with 304.** The conditional request via `If-None-Match`,
    zero traffic, `unchanged`, and an unconditional retry when the file has gone
    missing from disk are pinned — against a stub that issues the ETag itself.
    This has not happened against the real `serve_slot_download` with its ETag
    `"{fp}-{rep}"`.
24. **Scale and disk.** A mirror of dozens of reports and gigabytes has never
    been assembled: the ceiling has been checked only by refusing BEFORE the
    download based on the promised size (a 5 GB slot with a 4 GB ceiling); there
    has been no real ENOSPC on a full disk. The cost of `data.json` was measured
    synthetically (41 ms for 4.5 thousand files, 122 ms for 13.5 thousand), not
    on a live mirror.
25. **A residual risk to know before distributing the kit.** If creating the
    tray icon fails on the person's machine, the mirror PREVIOUSLY did not start
    at all — that is exactly what item 6 closes. But the fix can only be
    verified where the icon exists: on macOS the path WITHOUT the icon was
    verified, on Windows — not at all. Until that happens, item 6 is closed on
    macOS and not closed on Windows.

#### Pass of 12.10.2026: window, folders, paths

Three implementers worked in parallel against the contract in the RFC ("Window
and folders"), without seeing each other's files. Hence the form of this
record: what was carried out per the contract, where the contract was silent and
what they decided themselves, and what diverged at the seams.

26. **The library in a window, not a tab.** Before: `controller.open_library` →
    `webbrowser.open('http://127.0.0.1:<port>/library/')`. After:
    `appwindow.open_window(url, state_root=, title=)` — `msedge.exe --app=<url>
    --user-data-dir=<state>/window --window-size=1280,860 --no-first-run`, the
    process is detached and the agent does not wait for it; if there is no Edge
    or it fails to launch — the old `webbrowser.open`, the answer `"browser"`,
    the reason in the log. Reverse run: putting `webbrowser` back into the
    controller gives 3 FAILs in `smoke_appwindow`. The `--app` window does not
    accept a title — it takes it from the page's `<title>`; per the contract,
    the `title` parameter goes only to the log.
27. **Shortcut and icon.** `setup.bat`, step `[7/7]`: "OnePage — my reports" on
    the desktop and in the Start menu via `WScript.Shell`, without
    administrator rights; the name is assembled from character codes (the
    `.bat` encoding), paths go through an environment variable, a shortcut
    failure is a warning, not an aborted install. The target is
    `cmd /c op-agent.bat open` in a minimized window: the subcommand waits up to
    20 s for `/health` across `PORT_LADDER`, then calls
    `appwindow.open_window` (why not `pythonw` directly — see the comment in the
    `.bat`). The icon `deploy/agent_tools/onepage.ico`:
    `tools/make_app_icon.py --check`, PNG inside ICO 16/32/48/256, two runs
    produce an identical file; `make-agent-kit.sh` checks it and copies it
    byte for byte. `--check-kit` in `build_agent_single.py` does NOT know about
    the icon.
28. **Folders by path; where the contract was silent, this is what was
    decided.** `folders.ini` next to the program, `[folders:<id>]`, repeatable
    `dir`/`file`. `include`/`exclude` are SECTION keys (in the ini a mask cannot
    be bound to a specific `dir` line) and apply only to folder enumeration: a
    named `file` is read as named, otherwise a mask would silently swallow an
    explicitly specified file. There are exactly four `Missing` reasons per the
    contract, and "this is not a file" is not among them, so a `file` pointing
    to a directory yields `not_a_dir` ("the path type is not the declared
    one"); a fifth reason was not introduced — it would have broken the
    contract for the neighbours. A section without `dir`/`file` →
    `Missing(raw="[folders:<id>]", reason="not_found")`, so that the invariant
    "an empty `files` only with `missing`" has no hole. An invalid mask (`/`,
    `..`, wildcards only) is not discarded — there is nothing here to execute
    `validate_pattern` with — but by construction it matches no flat name, and
    the person gets an honest `no_match`. UNC gets a separate normalization
    branch not as a special case but because on POSIX `os.path.abspath`
    considers `\\server\share` relative and prepends the CWD: `identity` would
    depend on where the program was launched from. An unknown `errno` (`EIO`,
    `ESTALE`, a share not responding) → `not_found`, not `denied`: "denied"
    would send the person off to investigate permissions they never lost. A
    broken ini → an empty dictionary, not an exception: a crashed program
    cannot complain. `default_ini_path()` (`folders.ini` next to the program,
    `OP_FOLDERS_INI` for tests) was added beyond the contract.
29. **CLOSED: the name `open` shadowed the builtin.** The contract name
    `appwindow.open` became a top-level `def open` in the flat concatenation,
    and everything concatenated after it got the window-opening function
    instead of the builtin `open`: `open(path, encoding="utf-8")` in the mirror
    store failed with `TypeError`. The kit still BUILT and compiled — the error
    lay in wait for launch. The function was renamed to `open_window` (the
    contract in the RFC was corrected), and the builder got
    `_assert_no_shadowed_builtins`: such a name now stops the build with an
    explanation instead of surviving until it reaches the person's machine.
    Verified by a reverse run: with the name `open` the build refuses;
    `smoke_mirror_kit` is 23/23 again.
30. **CLOSED 19.10.2026: `folders.py` is wired in.** Before: neither the
    schedule, nor the window, nor sending `path`/`root_path`/`root_id` into the
    observations called it — only `smoke_folders` (48); meanwhile the server
    already accepted paths (item 31), and the seam existed on one side only.
    After: it is called by `runs._resolve_folder_files` — the R2 run branch for
    a bundle without `sources` (item 43) — and by `server.folders_payload` for
    `GET /folders?dashboard=…[&scenario=…]` (contract K1: the same `load`, the
    same `<id>[:<scenario>]` key, the same order of refusals and the same
    duplicate count as the run — showing one list and then refusing for a
    different reason is the worst outcome short of an empty run). The response
    carries `files[].path`, `roots: [{dir, root_path, root_id}]`, `missing`,
    `duplicates`; the page puts this into the `input_manifest` of the `local`
    run (K3). What is NOT verified: that a real path appeared on the data-source
    page of a real server — there has been no live run against the platform.
    The schedule still does not call `folders.py`: there are no overnight runs
    (the order is unchanged — after the gate on the numbers, item 44).
31. **Paths on the platform — the server accepts and shows them.** `POST
    …/runs/{id}/observations`: `files[].path` both in the start manifest and in
    a batch (the batch wins; ≤512), `roots: [{dir, root_path, root_id}]`
    (`root_id` ≤32); a non-string and a garbage `roots` are silently ignored
    (the fields are `Any`, so that a 400 does not cost the batch); an absent
    field does not erase a previously known path. **This is the implementer's
    interpretation — the RFC did not specify it**: when the application starts
    sending, it must send exactly this way, or the seam will diverge. Columns
    `source_files.path`, `data_sources.root_path/root_id`, Alembic `0009`
    (additions only, no indexes on purpose). Display: `/d/{id}/data-sources`,
    the source drawer, `/d/{id}/provenance` (page and JSON), the lineage node,
    the "Copy path" button (`static/js/source-paths.js`). The right is the same
    `can_update_dashboard_data`; a report reader does not see paths, and a path
    never reaches the model's prompt (pinned by a reverse run: a line with
    `root_path` in `nl2sql.py` turns the test red).
    `tests/smoke_source_paths.py` 68.
32. **The caveat "folders are matched by name" is half lifted.** For the
    browser it holds as before. For the application `root_id` exists but does
    NOT take part in grouping sources: `DataSource` is looked up by folder name,
    and two same-named folders on different paths will merge into one source
    with the path of the latest run. The data-source page says so honestly
    ("sources are still grouped by folder name … compare the root id"). Lifting
    it fully is the owner's decision about the source key; it changes the
    schema.
33. **The port and the unsigned `pythonw.exe` remain.** The window is Edge on
    top of the same loopback server. WebView2 with
    `SetVirtualHostNameToFolderMapping`, where there is no port at all, is the
    next step, not this one (RFC, "What this does NOT solve"). The antivirus
    risk from the RFC has not changed one bit.
34. **CLOSED: `server.state_root` could be `None`.** An empty field sent the
    window into the fallback tab over a trifle (nobody guaranteed the
    initialization order). `controller.open_library` takes
    `self.server.state_root or state_dir()` — the same state directory the agent
    itself uses.
35. **`op-agent.bat open` relies on names from the built `op_agent.py`**
    (`a.applog`, `a.appwindow`, `a.mirror_schedule`, `a._is_our_agent`,
    `a.PORT_LADDER`, `a.state_dir`). The Python one-liner has only passed a
    syntax check. The `.bat` is stored in the repository with LF; CRLF is
    produced by the `perl` loop in `make-agent-kit.sh`; the script itself has
    only been checked with `bash -n` — it needs a run on a machine with internet
    access.

#### Not verified as of 12.10.2026 — and must not be passed off as verified

36. **Nobody has tested on Windows yet**, and this now applies to the new parts
    as well: launching Edge `--app`, reading the App Paths registry, creating
    the shortcut via `WScript.Shell`, `casefold` in `identity`, UNC to a live
    share, `denied` via ACL (on macOS it was checked with `chmod 000`), the
    behaviour of `scandir` on SMB. The UNC tests on POSIX check normalization
    and the key, but not reading.
37. **The platform side has not been fully caught up.** Green:
    `smoke_source_paths`, `smoke_lineage`, `smoke_migration`,
    `smoke_provenance`, `smoke_i18n`, `smoke_datasources`,
    `smoke_lineage_ui/py/backfill`, `smoke_docs`, `node tests/lineage_dom.js`,
    `i18n_dom.js`; `i18n_check` — clean. Not waited for: `smoke_generator*`,
    `smoke_quality`, `smoke_catalog`, `smoke_xss*`, `smoke_share*`,
    `smoke_observ*`. Nobody has clicked the "Copy path" button in a browser,
    and the Chrome harness has not been run. `smoke_mirror_kit` is green
    (23/23) after item 29; the agent starting up from the kit on Windows was
    verified by the owner — and that exposed item 38. The other agent smoke
    tests are green on macOS: `smoke_agent` 37, `smoke_zipguard` 7,
    `smoke_mirror` 89, `smoke_mirror_sync` 98, `smoke_mirror_wiring` 99,
    `smoke_tray` 54, `smoke_folders` 48, `smoke_appwindow` 30.
38. **CLOSED (after a live failure on Windows, 12.10.2026): polling our own
    port went THROUGH the system proxy.** The owner installed the kit and
    clicked the shortcut — exactly what the shortcut is for — and got "the
    agent did not respond within 20 seconds". The names in the concatenation
    were intact, the window was built, the agent started: it was the poll that
    lied. `urllib.request.urlopen` reads the environment's proxy settings (on
    Windows — from the registry), and `<local>` in the exception list **does not
    cover 127.0.0.1**: by CPython's rule it matches only a host name WITHOUT
    dots. On a machine with a corporate proxy, a request to our own loopback
    went to the proxy and came back refused, which for us means "no agent". The
    cost was twofold: the shortcut waited 20 s for a live agent, and the
    single-instance mutex broke — a second instance did not exit silently but
    took the next port of the ladder. Fixed in BOTH entry points
    (`agent/__main__.py` and the tail of `deploy/build_agent_single.py`):
    `build_opener(ProxyHandler({}))`. Checking our own port is never an
    outbound request. Pinned by `smoke_agent` with two checks: with a dead
    proxy the agent is found AND a naive `urlopen` really does fail at the same
    time (without the second half the test is green and proves nothing).
    Syncing with the platform still honours the proxy — that is the request
    that actually goes outside.
39. **A shortcut failure no longer sends you to a tray icon that does not
    exist.** Before: "open the log from the op-agent tray icon menu" — yet when
    the launch fails, there is precisely no icon. Now, on failure,
    `op-agent.bat open` calls the new subcommand `op_agent.py diagnose`: the
    folder, the state, the config, kit integrity, every port of the ladder with
    a **raw socket** (separately from `/health` — only this way can "the agent
    did not start" be told apart from "it started, but the poll lied") and the
    last 30 lines of the log; with no log — direct advice to run
    `op-agent.bat --no-tray` and send the output. The command exists only in
    the kit: its entry point is richer (`set-cred`, autostart, `check-kit`),
    and the two entry points cannot be merged into one.
40. **CLOSED (after the owner's complaint, 12.10.2026): "Updating copies…" and
    not a single sign of movement.** The status line did not distinguish three
    different stages of a pass and showed the same phrase throughout: while the
    manifest request is in flight (nobody knows the number of reports yet, and
    no counter exists), while the first file is downloading and while a
    twenty-megabyte report is downloading — in all three cases the person read
    "Updating copies…" or a motionless "3 of 12". Progress did exist — the bar
    on the library page and the report counter — but they stood still exactly
    during the minutes when the question "is it alive?" comes up. Now: a
    separate key `sync_manifest` for the pre-manifest stage (the stage is
    named, no bar is drawn — a fraction of an unknown must not be promised) and
    `sync_running_bytes` with the amount downloaded. Bytes are counted INSIDE a
    file: `_StoppableReader` got `on_chunk`, `_read_body` ticks per chunk, and a
    tick fires at most once per half megabyte. The callback contract became
    three-argument (`progress(done, total, downloaded)`); the third argument of
    `_on_progress` has a default value — the kit and the repository are
    updated by different means, and a two-argument call has no right to bring
    down the schedule thread. Pinned by `smoke_mirror_sync` (bytes never
    decrease, are non-zero by the end, three stages — three different strings)
    and by a new check in `smoke_mirror_wiring`: every `U.fmt('key')` from
    `web/*.js` exists in `strings.json` — a miss on such a key neither crashes
    nor gets logged; the page simply prints the key itself.
41. **An S3 bucket as a network folder (`agent/s3sync.py`, 14.10.2026).** The
    owner's request: "so that the generator can read from an S3 bucket like
    WinSCP — I specify the bucket, the folder, the login and the password;
    there will be Excel files there". It was done exactly like WinSCP, and this
    is the main design decision: the agent DOWNLOADS the bucket prefix into a
    local folder, and after that nothing changes — the same folder selection in
    the engine, the same masks, the same lineage. No second data source
    bypassing everything else has appeared. Why not in the browser and not on
    the server: the storage key cannot be handed to the page (it would leak
    into the browser session), and the OnePage server does not go to the
    internet at all and does not hold analysts' credentials.
    A `[s3:<name>]` section in `agent.ini` (endpoint, region, bucket, prefix,
    access_key, target, path_style, include/exclude); the secret is in DPAPI
    under the name `s3-<name>` (a hyphen, not a colon: the name becomes a file
    name, and on Windows a colon is the NTFS stream separator). The commands
    exist only in the kit: `s3-list <name>` (an access check that downloads
    nothing — the first question on someone else's machine is "do we get
    through, and is it the right key", and answering it with gigabytes is
    wrong) and `s3-sync <name>`.
    **The SigV4 signature is our own; there is no boto3**: the kit is
    installed offline from four wheels, and `boto3`+`botocore` are tens of
    megabytes for the sake of two requests. The price is named and paid: four
    reference signatures in `smoke_s3sync` were checked against
    `botocore.auth.S3SigV4Auth` (1.42.97) and frozen, and when botocore is
    present on the developer's machine the test re-checks against it. Two traps
    this comparison exposed: the path in the canonical request is taken AS IS
    (S3 is the only AWS service that does not normalize it; "fixing" `%7E` →
    `~` broke the signature), and the region is part of the key's credential
    scope, so a wrong region gives a 403 without explanation — a dedicated
    refusal text says so.
    **We delete only what we downloaded ourselves** (state in
    `state_dir()/s3/`): a stale file in the generator's folder is more
    dangerous than a missing one (the engine will read both it and the new one
    — a silent doubling of data), but the program has no right to clean
    someone else's folder; foreign files are left alone and listed in the
    summary. An interrupted download leaves no file (a check against
    `Content-Length` + writing via `.op-s3-tmp`), and a key with `..`/a
    backslash/a colon is rejected BY NAME.
42. **CLOSED (a defect found in the built kit, 14.10.2026): the entry point
    called a module by name, but the module had no view.** Views
    (`_FlatModule`) are built from the relative imports of the modules
    THEMSELVES, and a module used only by the `_MAIN` tail got no view:
    `s3sync.load_s3()` built, compiled, passed `--check-kit` and failed with
    `NameError` on the very first run of the command. The same class of bug as
    builtin-name shadowing (item 40 in CLAUDE.md), and it is cured the same
    way: an `ENTRY_VIEWS` list in the builder plus `_assert_entry_views`, which
    STOPS the build with a ready-made explanation. Verified by a reverse run:
    an empty `ENTRY_VIEWS` makes the build refuse, rather than producing a
    silently broken kit.

#### Pass of 19.10.2026: native folder run

43. **Two generator runners, and they are two DIFFERENT contracts — the code
    used to say the opposite.** The header of `agent/generator.py` said "one
    convention for the browser and the agent". That was untrue: the browser
    executes the R2 contract (`begin/add_file/finish`,
    `docs/GENERATOR_AUTHORING.md` §5), while the agent ran
    `python -m <module> --input-dir … --output-dir … --schema-version …` — a
    command line that is documented nowhere, is not checked in
    `genmanifest.validate()` and is implemented only by slreport. The mismatch
    between documentation and code took a separate investigation, so it is
    recorded as before/after rather than fixed silently. After — two runners
    with named roles:
    * `generator.run_r2_subprocess` — **R2**, the main path for generators that
      read folders, and the only one that produces the same numbers as the
      browser (verified by the gate, item 44). The executor is
      **`agent/r2host.py`**: a separate process under ordinary CPython with
      `-I` (the host itself puts the bundle on `sys.path` — that is why
      `--app-dir` is a separate argument; nothing is imported from the
      analyst's folders), line-delimited JSON in both directions (`add_file`,
      `finish`, `abort`, `ping` ↓; `ready`, `progress`, `disposition`,
      `warning`, `result`, `error` ↑). Frames go to a duplicate of fd 1, while
      the real fd 1 is redirected to stderr (`os.dup2(2, 1)`): the generator is
      allowed to print, pandas and calamine print on their own from C code,
      and the very first such line on fd 1 would break parsing; Windows has no
      `pass_fds`, and that is where the kit goes. `begin()` is called before
      the first command — a failure at import arrives as an `error` frame, not
      as a dead pipe. A module without `begin()` — refusal `gen_no_r2`, with NO
      fallback to legacy: a different launch = a different cache, different
      warnings, and in the worst case different numbers.
      In the kit, `r2host.py` sits as a SEPARATE file next to `op_agent.py`
      (`HOST_FILES` in the builder): it is launched as a script and cannot go
      into the concatenation; it is looked up next to the module and next to
      `sys.argv[0]`, and the `gen_no_host` refusal prints every place it
      looked.
    * `generator.run_generator_subprocess` — **legacy CLI**, kept solely for
      the GreenPlum path (`sources` in the manifest, files put into the inbox
      by `gpsource`). Not promised to new generators.
    `runs.run_generator` picks the branch by the presence of `sources`. Before
    this pass, a bundle without `sources` got an EMPTY inbox and built a report
    out of nothing — silently.
    Four host decisions, each with a comment at its own function in
    `r2host.py`: `path` is the analyst's REAL file — the host deletes nothing,
    renames nothing and keeps nothing open, the disposition is informational
    (`_add_file`); `mtime_ms`/`size` are re-read before `add_file`, the input
    set is not frozen — OneDrive and the person edit workbooks during the run,
    and a mismatch produces `input_changed`/`input_modified` warnings
    (`_stat_file`); a `.r2host-running` marker in `cache_dir` is set before
    `begin()` and removed only after a successful `finish()`; if found at
    startup, the cache directory is wiped entirely with a `cache_reset`
    warning, because a half-written entry is read as `cached` and silently
    produces a wrong report (`_arm_cache`); stdout is taken (`_Frames`). What
    of this is NOT verified: Windows has not been seen at all — locks on open
    workbooks, `dup2` without a console, OneDrive paths; `write_back` natively
    reaches `out_dir` and is handed to the page, but writing back into the
    analyst's folder is neither implemented nor tested.
44. **The gate on the numbers was passed with status WARN — for one
    generator.** `tools/engine_parity.py` + `tools/run_bundle_pyodide.mjs` run
    one bundle on one folder twice — in Pyodide under node and natively
    through the same `r2host.py` — and compare the outputs in layers: bytes
    (the only PASS), then decoding and numbers (WARN when all numbers match
    within a 1e-12 tolerance and the dispositions match), an opaque format
    without `--decoder` — FAIL. slreport 2.2.25, 36 synthetic workbooks: the
    slot is byte-identical, 3527 numeric literals in the html matched, only the
    timestamp and the input folder path differed. A probe confirms the
    divergence mechanism is real: `np.array([1]).dtype` is `int32` in wasm
    versus `int64` natively, `sys.maxsize` 2147483647 versus
    9223372036854775807, the sum 8×2³⁰ is 8589934592 natively and 0 in wasm;
    pandas holds `int64` explicitly, the risk is bare numpy without `dtype`.
    This proves parity of ONE generator on ONE dataset; it has to be run on
    every bundle that will go into a native run. The opaque slot was compared
    by bytes only — there is no decoder for the slreport format. A side finding
    of the gate: slreport puts the input folder path into `dashboard.html` —
    `/inbox` in the browser, the real path on the person's disk natively,
    visible via a share link; the rule for authors is written in the guide
    (§5, "Native run"), the engine does not check it. The comparison layer is
    pinned by `tests/smoke_engine_parity.py`.
45. **Scenarios in `folders.ini` — the key exists, the UI does not.**
    `[folders:<id>]` is the default set, `[folders:<id>:<name>]` is a scenario
    (`Source.scenario`, `Source.key`, `folders.scenarios_for`); we split on the
    FIRST colon — a report id is a UUID and contains no colons. Needed for
    scenario planning: one report, several sets, comparison through platform
    versions. The run picks the set by `meta.scenario` (an unknown one — refusal
    `folders_no_scenario` listing the known ones, a missing section —
    `folders_no_section`), and `GET /folders` accepts `&scenario=`. There is no
    selection UI yet — the owner's decision of 19.10; as soon as it appears,
    the run already does everything needed.
46. **The kit ships pandas/numpy/calamine and moved to CPython 3.14 — versions
    come from the Pyodide lock, not from the generator manifest.** Before:
    `make-agent-kit.sh` put only `psycopg[binary]`, `tzdata`,
    `typing-extensions` and a relocatable CPython 3.12.10 from nuget into the
    kit — the native folder run (item 43) was physically impossible, there was
    nothing to read workbooks with. After — two decisions, both taken against
    the "obvious":
    * **The versions of the analytics packages come from
      `app/services/data/pyodide_lock_314.json`** (pandas 3.0.2, numpy 2.4.3,
      python-calamine 0.6.2), and NOT from the manifest's `deps`. For
      distribution packages the browser ignores the manifest pin altogether —
      micropip installs the distribution's version, hence the
      `wasm32_note: pyodide dist ships X` in `genmanifest.resolve_deps`. So had
      we built the kit "by the manifest", the native run would compute on a
      different pandas version than the browser: building to match the
      manifest would have GUARANTEED that the engines diverge. There is one
      source — `deploy/agent_tools/lock_specs.py`, and its list feeds THREE
      consumers: `pip download` in the builder, `check_wheels.py` on the built
      kit, and `wheels/requirements.txt`, which `setup.bat` installs from (the
      list is deliberately not written into the .bat — that would be a fourth
      copy of the package set, which would one day diverge).
    * **CPython 3.14.8 instead of 3.12.10.** In the browser the engine runs
      under CPython 3.14 (Pyodide 314.0.2), and the Python minor version is a
      whole axis of numeric divergence. win_amd64 cp314 wheels were found for
      all five packages (verified by downloading), so the axis is removed for
      free. The nuget.org package `python/3.14.8` carries `tools/python.exe`,
      `pythonw.exe` and `ensurepip` with pip 26.2.1 — installation on the
      analyst's machine still needs no network.
    Sizes of the built kit (20.10.2026): zip **41 MB**, unpacked **43 MB**, of
    which wheels are **26 MB** (9 of them, numpy 11.9 + pandas 9.4 + calamine
    0.7 + psycopg_binary 3.6) and the relocatable python 16 MB.
    **`gen_missing_dep`** is a new kind of refusal in `r2host.py`
    (`_check_deps`, the host's fifth decision): a manifest that declared a
    package not in the kit stops the run AT START, naming the package, and —
    importantly — BEFORE `_arm_cache`, because that sets the marker and wipes
    the previous run's cache; a refusal after it would cost the person a full
    recomputation over a typo in the manifest. A version mismatch with the
    installed one is a `dep_version` warning, NOT a refusal: the browser only
    notes it at this point too, and strictness here would mean the engines
    diverge in strictness.
    **Not verified**: nobody has seen Windows yet — neither unpacking the 3.14
    nupkg, nor `pip install` from `wheels/` on a real machine, nor importing
    pandas 3.0 under cp314 on Windows; it has only been verified that wheels
    with these tags exist, download and close over their dependencies
    (`check_wheels.py` on the built kit — ALL PASS).

47. **Offline run and the catch-up queue (19.10.2026, phase 7B).** Before: the
    `local/L<k>` space existed in `layout.py` in full — paths, numbers,
    `add_local_run`, `can_update_locally`, URL parsing, the `local_runs` field
    in state.json, and `prune_versions`, which deliberately leaves it alone —
    and was wired to NOTHING: not a single line of code wrote there. A run was
    possible only with a live OnePage page, i.e. exactly when it is not needed.
    After: `mirror/localrun.py`, `POST /library/local-run` (the "Update from
    folders" button on a report where this is allowed), agent endpoints
    `GET /local-runs`, `GET /local-runs/output`, `POST /local-runs/published`,
    a bundle cache in `sync._cache_bundle`, and the labels
    `fresh`/`outdated`/`local_only`, which until then lay dead in
    `strings.json` (`_mirrored_item` answered `not_downloaded` or nothing).
    Checks: `agent/tests/smoke_localrun.py` (78), including a real
    `AgentServer` serving `/mirror/<id>/local/L2/` and its slot.
    **What is worth knowing about the internals.**
    * **The bundle is cached during sync, under the name of its own sha256**
      (`<mirror>/<id>/bundle/<sha>.zip`), and only for reports with the right
      to update. Otherwise there would be nothing to recompute offline with:
      the bundle lives on the platform. The checksum is verified after writing
      — a cache that holds something else under a checksum's name lies to
      everyone who looks into it.
    * **The data-feeding bridge is borrowed, not copied.** The generator
      returns bare html; an interactive report holds no data inside, it is fed
      by `op_ready`→`op_file`, and that script is inserted by the SERVER
      (`detector.inject_bootstrap`). So the local document gets the same block,
      cut out of the already downloaded server copy, with the slot inventory
      substituted. Nowhere to take it from (no server copy, the marker was
      renamed) → `bridge: "none"`, the run is not cancelled, and the library
      and the viewer say the report will show empty sections. The marker is
      checked against `app/services/detector.py` by a smoke test — the kit has
      no such check and cannot have one.
    * **The right is time-limited, and that is the only protection.** A right
      revoked on the platform arrives only with the next sync; an expired
      `rights.expires_at` STOPS the run and does NOT prevent reading (RFC §5).
      An RLS report is rejected at three lines of defence: it is not in the
      mirror, `precheck` answers `rls`, serving is closed.
    * **Catch-up is not automatic, by the owner's decision.** The question
      "publish to the platform?" appears in the library only after sync has
      SUCCEEDED AGAIN; until then — a calm line "numbers saved". The OnePage
      page does the publishing: no new server endpoint was added; the page
      creates an ordinary `engine:"local"` run with the SAVED `input_manifest`,
      fetches the outputs over loopback and publishes. The price is that
      `started_at` will be the time of the catch-up — which is why the version
      note carries BOTH dates (`localrun.publish_note`), not one.
    **Not done, and said so.** There is no offline write-back into folders: the
    names of such outputs are recorded in the run in the `write_back_skipped`
    field, and files are not written into the person's folder (the program
    must not write into someone else's folders without the owner's decision).
    The endpoint accepts a `folders.ini` scenario choice (`scenario`), but the
    library has no selector — just as the page had none before this pass
    (item 45). The page side (`static/js/gen-engine.js`) was deliberately left
    untouched: catch-up there is done by the integration stage (item 48).
    Windows, like everything else in this section, has not been tested.

48. **Catch-up on the page and the lineage of a native run (19.10.2026,
    integration of phases 6 and 7).** Two changes, and the second matters more
    than the first.

    * **Catch-up is carried through to the platform**
      (`static/js/gen-engine.js`). When the panel opens, the agent probe also
      asks `GET /local-runs?dashboard=` — FIRST and separately from the
      folders: the run has already been computed, and it must be published even
      if its section has since been removed from `folders.ini`. There is an
      unpublished run → an inset with an explicit question and two buttons.
      "Publish" goes down exactly the same path as an ordinary run: `POST
      …/generator/runs` (`engine:"local"`, the SAVED `input_manifest`) →
      observations → outputs from the agent → `POST …/publish` → and only AFTER
      a successful publish `POST /local-runs/published`. No new server
      endpoints. "Not now" hides the inset and marks NOTHING: the run will ask
      again. A failed mark does not cancel the publish and is said out loud —
      otherwise the person would publish the same numbers a second time.
    * **The version note names the offline run.** The start manifest carries
      `offline: {created_at, k}`, and `app/api/generators.py::_run_offline_at`
      appends `· built offline <date>` (the code currently emits this label in
      Russian) to the version label next to `generated: run N` and the
      scenario. Without it the version
      would look as if it had been computed on the day of publishing — that is
      exactly what its `started_at` says.
    * **A hole in the lineage was found and closed.** The R2 host sent the page
      `{"t":"observe",…}` frames over NDJSON, and `onAgentEvent` silently threw
      them away: a run "on this computer" arrived on the platform without a
      single column read, even though the agent honestly named them. Now the
      frames go into the same queue as the browser engine's, and the final
      batch is sent BEFORE publishing. For catch-up, the frames travel together
      with the input — `localrun.read_observe` + the `observe` field in
      `GET /local-runs` (`with_input`): there is nothing to recompute them from
      at publish time, the files in the folders may have moved on by then.

    **Verified by an end-to-end run on macOS** (a clean stand, the slreport
    2.2.25 bundle, 36 synthetic workbooks): the mirror was synced, the stand was
    shut down, the generator ran natively (`local/L1`, "not published"), the
    stand was brought back up, catch-up produced version 3 with the note
    `generated: run 2 · built offline 2026-10-20`, the slot was 1 404 516 B,
    and all 36 files reached the lineage with paths, `root_id`, the sheet
    schema (5 793 rows) and the nine columns the generator actually READ.
    **Not verified:** Windows; nobody looked at the page in a browser — catch-up
    on it is pinned by the node test `tests/gen_local_dom.js`, not by a live
    click; `bridge` in this run came out `none` (the stand's server document
    was a stub), i.e. the `borrowed` branch has not been exercised end to end.

49. **An "Update from folders" button in the viewer header (20.10.2026).**
    Before: updating lived only in the LIST (`web/library.js`), and a person who
    had already opened a report and saw "Copy from 21.10.2026 01:02" in the
    header had to go back to the library — the owner asked "so where's the
    update button?" while standing inside a report. Now the action sits where
    the question arises: the header is drawn by `web/viewer.html` +
    `web/viewer.js` (the `mirror/serve.py` serving draws ONLY the document
    inside the frame and knows nothing about buttons — the two trust boundaries
    stayed in place, and there is still no key in `/mirror/`).

    There is ONE mechanism for both screens:
    `post`/`button`/`toast`/`errText`/`syncNow`/`updateNow` moved from
    `library.js` into `web/common.js`, and the list calls them from there too.
    There is still a single request — `POST /library/local-run` under the
    check of its own loopback Origin (`libui._same_origin`), no CORS added;
    `viewer.js` does not know this address at all (pinned by a test).

    The difference from the list is intentional: in the list, a forbidden update
    has NO button, while in the header the button is there and dimmed
    (`.btn.off` — not `[disabled]`, otherwise clicking it would do nothing).
    Clicking expands a strip under the header with the reason
    (`local_run_err_*` or the agent's own words for rare `folders.ini` codes)
    and with a WAY FORWARD: "Report folders" (`/folders-ui/`, which also has the
    "Browse…" button) for `folders_*` refusals, "Update now" for
    `rights_expired` and `no_bundle`. The progress of the run, its result and a
    link to the fresh local version are shown in the same strip; the report
    frame is NOT reloaded meanwhile — polling `data.json` (1.5 s, while the run
    is in progress) redraws only the header.

    **Not verified:** nobody has seen this header in a browser — neither on
    Windows nor on macOS; the checks are automated (`smoke_mirror_wiring` 132,
    `smoke_localrun` 84). Not exercised with a live click: the run itself from
    the header, the progress strip, the "Open version from …" link after a
    success, and navigating to `/folders-ui/` from the refusal strip. The header
    layout with a long report title and a narrow window (button + "Copy from …"
    in 40 px of height) has not been eyeballed either.

50. **The bucket is configured in a window, not in Notepad (20.10.2026,
    `agent/s3ui.py` + a panel on the "Report folders" screen).** Reading from
    the bucket was done back on 14.10 (item 41), but it could only be SET UP by
    editing `agent.ini` by hand. The owner came back to this with two
    screenshots of the WinSCP window and said plainly: let the bucket be
    configured in the same place GreenPlum is configured. So the panel's fields
    are exactly the ones he sees in WinSCP, and they map onto the existing
    `[s3:<name>]` section without a single new key:

        Host name + Port + Encryption (TLS) → endpoint  (the default port is NOT
            written into the address: it is part of the canonical request's
            `Host`, i.e. of the signature, and a superfluous `:443` would give a
            403 without explanation)
        Access key ID                       → access_key
        Secret access key                   → DPAPI, entry `s3-<name>`
        Remote directory                    → bucket + prefix
        (picked with the mouse)             → target
        plus region, path_style, include/exclude — as their own fields

    **The main substantive decision is parsing `Remote directory`.** In WinSCP
    the first segment of this field is the BUCKET, not a folder inside it (the
    Amazon S3 protocol shows the list of buckets as the root), but a person who
    was told "specify the folder" will write `prod-s3-nwd`, and
    `prod-s3-nwd/exports`, and `/prod-s3-nwd/exports/` there. Silent guessing
    is not allowed: a mistake here is invisible — the listing returns zero
    objects, and the person goes looking for a non-existent problem with the
    key. So there is one parser (`split_remote`), its result is shown as a line
    "Bucket: … Prefix: …" BEFORE saving, and the page has no parser of its own
    at all — it asks the agent (`POST /folders-ui/s3/preview`). The prefix
    always gets a trailing slash: without it `weekly` would also match
    `weekly-2025/`.

    **The connection test is real** (`POST /folders-ui/s3/test`): the same
    `S3Client.list_objects`, the same masks and the same `relative_name` as in
    `s3-list` and in the run — how many objects are visible, a few names, the
    total size, where it will be downloaded to. No downloading, and a ceiling of
    2000 objects: the answer to "do we get through and do we see the right
    thing" does not improve by enumerating hundreds of thousands of keys. A
    refusal arrives as an `S3Client._explain` phrase (a wrong region and a wrong
    `path_style` produce 403/301 responses that look identical — so we name
    them), and no branch shows a traceback.

    **Boundaries.** The secret goes into DPAPI and appears nowhere else: it is
    not in `agent.ini`, not in the endpoint's response, not in the log — the
    only thing that goes out is a `secret_set` flag (the same write-only
    discipline as the git token and the model keys on the platform). An empty
    secret field does NOT erase the saved one: the form sends it empty every
    time the masks are edited, and "empty means delete" would lose the secret
    out of the blue — deletion has its own button. The POSTs pass the same
    own-loopback-Origin check as the others on this screen (`foldersui._post`),
    no CORS added. There is no `print` or `applog` in `s3ui.py`, and this is
    pinned by a test: the path to the person's folder and the names of the
    objects in their bucket come through here. The destination folder is
    chosen with the existing "Browse…" button (`POST /folders-ui/pick`); there
    is no second way to name a folder on the same screen.

    Writing is line-based, as with `folders.ini`: `agent.ini` is also edited by
    whoever configures GreenPlum, and their comments are the only place where
    the reasons for the settings are recorded. The shared facts of ini syntax
    (`raw_sections`, `option_key`, `rebuild`) became public in `foldersio` — two
    editors of two files must give the same answer to the question "where does
    a section start".

    **Not verified:** everything the same as for item 41, and a bit more.
    Nobody has a real Windows — DPAPI has never been executed (on macOS a dev
    backend works under `OP_AGENT_DEV_CRED_STORE=1`), the Edge `--app` window
    with this panel has not been eyeballed, nor has the form layout in a narrow
    window. There has been no real Yandex Object Storage (nor any live S3): the
    signature was checked against botocore but has never gone to
    `storage.yandexcloud.net`, and the `region`/`path_style` values for which
    Yandex answers 200 are not confirmed by experience — the panel only names
    the refusal in plain words. The checks are automated: `smoke_s3sync` 82 (83
    with botocore), `smoke_foldersui` 106.

51. **The bucket became the report's SOURCE, not a command next to it
    (20.10.2026, `agent/runs.py::_pull_buckets`, `s3sync.connections_for`,
    `s3ui.pull_now`).** Before: downloading lived only in `op-agent.bat s3-sync
    <name>`, typed by hand, and the run knew NOTHING about the bucket — not a
    single reference to `s3sync` in `runs.py`. If nobody refreshed the folder,
    the generator honestly recomputed the report on yesterday's workbooks and
    suspected nothing. That was the main defect: a quietly wrong report is
    worse than a refusal, because people find out about it a week later and
    from somewhere else.

    **The link is expressed by the FOLDER, not by a third field.** The person
    has already said in `folders.ini` which folder the report reads from, and in
    `agent.ini` where to put things from the bucket; if it is the same folder,
    the bucket is the report's source. `s3sync.connections_for` checks this by
    the normalized absolute path (`folders._norm`, the same one used to compute
    `folders.identity`; `casefold` only on Windows), not by string comparison:
    `D:\Exports`, `d:/exports/` and `D:\Exports\.` are one and the same
    folder. The tree is considered shared in BOTH directions (the target inside
    the report folder and the report folder inside the target — both cases are
    real), while a neighbour by name (`Exports-2026`) is not pulled in.

    **The refusal policy is named.** `_pull_buckets` is called from
    `_resolve_folder_files` BEFORE `folders.resolve`, i.e. before a single file
    is read, and on both run paths at once — from the platform page and from
    the offline "compute on this computer" (both call
    `_resolve_folder_files`). The bucket did not answer —
    `RunError("s3_unavailable")` with a human-readable `S3Client._explain`
    phrase and a way forward: what to untick in order to compute anyway. No
    secret — `s3_no_secret`, and the refusal leads to the "Report folders"
    screen, not to the documentation. Computing on what has already been
    downloaded is possible through two DELIBERATE actions: untick the
    connection's "refresh before run" box (`refresh_before_run`, ON by default
    — that is the whole point of connecting the bucket) or send a one-off
    `s3_stale_ok` in `meta`. Both cases are loud: a `warning`
    (`s3_not_refreshed` / `s3_stale`) goes into the event stream; there is no
    silence in any branch here.

    **"Download now" and the time of the last download.** The button in the
    bucket panel calls `POST /folders-ui/s3/pull`, which answers IMMEDIATELY
    and downloads in a background thread, while progress arrives by polling
    `GET /folders-ui/s3/progress` — exactly like "Update now" in the report
    mirror and for the same reason: a request hanging for a minute of
    downloading is indistinguishable from a frozen program. The `progress`
    contract is shared with the mirror, `(done, total, downloaded)`, so the bar
    moves even within one large workbook. The time of the last download is
    stored IN THE SAME PLACE as the state of what was downloaded
    (`state_dir()/s3/<name>.json`, fields `at`/`ok`): "what we have" and "when
    we got it" are one record. A failed attempt is recorded too — "downloaded
    today at 9:40" after a refusal would read as a success.

    **Not verified:** there has still been no live S3 (neither Yandex Object
    Storage nor any other) — the whole path was run against a fake store in a
    test; Windows, DPAPI and the Edge window with this panel have not been
    eyeballed; the behaviour on a bucket with hundreds of thousands of objects
    and on an export of gigabytes (how long the run sits at the `s3` stage, and
    whether the person's patience survives it) is not confirmed by experience.
    Automated checks: `smoke_s3sync` 115, `smoke_foldersui` 113,
    `smoke_r2folders` and `smoke_localrun` green.
