# Desktop program texts — where each string is shown

The file `strings.json` is a flat object, key -> English string. Parameters
in curly braces `{p1}`, `{p2}`, as in `app/locales/ru.json`. Substitute with
plain `str.replace`/`format_map`; there is no HTML inside the strings, so the
code that inserts them must escape.

There is no language switch in the desktop program. `strings.json` is
English. Next to it, `strings.ru.json` preserves the former Russian pack; the
program does not read it and it is not shipped in the kit
(`deploy/build_agent_single.py` copies only `strings.json` and `web/`). It is
kept so that a Russian build can be made by swapping the file.

Why a separate file and not literals in the code: the texts are edited by the
product owner, not a developer, and they must be able to do it without
reading Python. The second reason is tone. A person opens the program when the
platform is down and they urgently need the numbers. Every string here must
take the alarm away: first "what is with your reports", then "what to do". No
apologies, no "oops", no internal platform words (slot, fingerprint,
manifest, RLS).

Required context: `docs/RFC_DESKTOP_MIRROR.md` and the section "Desktop
report mirror" in `CLAUDE.md`.

If the file is not next to the program (the kit was copied without the
`mirror/` directory), the labels are replaced by KEY NAMES, and the page shows
a red bar about it — its text lives in `agent/mirror/libui.py` (`_KIT_NOTICE`),
because by its meaning it cannot lie in `strings.json`. A single key without a
string is shown by name too: the tray icon behaves the same way
(`schedule.text`).

## Library window

| Key | Where | Parameters |
|---|---|---|
| `library_title` | Window title and the page `<h1>` | — |
| `library_subtitle` | One line under the title. Explains that these are copies and that they work offline — the main thing a person must understand in a second | — |

## Sync status line

One line above the list. Exactly one key is shown at any moment.

| Key | When | Parameters |
|---|---|---|
| `sync_manifest` | The request to `/api/sync/manifest` has gone out, no answer yet: how many reports to update is unknown, and the counter does not exist at this moment. A separate key and not `sync_running`, because "Updating copies…" without a number is read by a person as a stuck download | — |
| `sync_running` | Downloading, the report counter is known, but bytes have not started yet (the first file request is in flight) | — |
| `sync_running_progress` | Downloading, the counter is known, but the bytes are unknown (the caller gave only a pair of numbers) | `{p1}` — how many reports are done, `{p2}` — how many in total |
| `sync_running_bytes` | Downloading and the bytes have started. The main progress key: the report count stands still all the time a large report is downloading, while the megabytes grow, and only by them can one see the program is alive | `{p1}`, `{p2}` — as above, `{p3}` — how much was downloaded in this pass, already formatted (“24 MB”) |
| `sync_done` | The last sync succeeded | `{p1}` — when: relative time from the `time_*` block or a date with time `DD.MM.YYYY HH:MM` |
| `sync_server_unavailable` | Any `urllib` network error (timeout, connection refused, 5xx). The program's main message — it is what a person sees on a bad day, so it says right away that the copies open | `{p1}` — in how long the next attempt, from `time_in_minutes` |
| `sync_no_key` | There is no key in the `creds.py` store | — |
| `sync_key_expired` | The server answered 401 and the key worked before. The key lives 90 days (`devicetoken.DEFAULT_TTL_DAYS`), and the program cannot tell "expired" from "revoked" — the text fits both cases | — |
| `sync_key_rejected` | The server answered 401 to a key that was never accepted yet (typed with an error, cut off when copying) | — |
| `sync_schedule_off` | The update schedule is not working: the program started without the icon (then `MirrorWorker` is not created at all) or the worker has no synchronizer — `data.json` gives `sync.enabled = false`. The line is shown INSTEAD of all the others and there are no buttons next to it: there is nothing to start an update with in this state, and a button that stays silent is worse than none | — |
| `sync_no_disk_space` | `OSError` ENOSPC when writing `.tmp`, or by the `size` estimate from the manifest before downloading | `{p1}` — how much is missing, already formatted (“1.2 GB”) |

## Report state in the list

A short caption under the report title. The comparison is by the manifest's
`fingerprint` against the stored one; "outdated" means the fingerprint
diverged and the copy has not been re-downloaded yet.

| Key | When | Parameters |
|---|---|---|
| `report_fresh` | The copy matches the platform as of the last successful sync | — |
| `report_as_of` | The platform is unavailable, freshness cannot be checked. We show the honest date of the copy, without judgements | `{p1}` — date and time of the download |
| `report_outdated` | The platform is available, a newer version is on it, the copy has not been updated yet (for example, there was not enough space) | `{p1}` — date and time of the copy |
| `report_local_only` | The data was updated by a local run (`local/L<k>`) and not published to the platform. Since 19.10.2026 the caption is live: `libui._mirrored_item` sets it for unpublished runs, and on the card the same caption sits on the "open the local version" link | `{p1}` — date and time of the local update |
| `report_not_downloaded` | The report is in the manifest with `mirrorable: true`, but there is no copy on disk yet | — |

Since 20.10.2026 the state is shown in TWO lines, and that is not a
duplicate. The report list became a table, like "Dashboards" on the
platform: the "State" column holds a badge of one or two words, and the long
phrase from the table above moved next to the title — an explanation in small
type. A table row cannot hold one long phrase, and a green "Up to date" on
all twenty rows is not information but noise: everything highlighted means
nothing highlighted.

| Key | Where | Parameters |
|---|---|---|
| `report_state_outdated` | Badge for `outdated`; the explanation with the date is given by `report_outdated` | — |
| `report_state_local` | Badge for `local_only`; the explanation is `report_local_only` | — |
| `report_state_absent` | Badge for `not_downloaded`; the explanation is `report_not_downloaded` | — |
| `report_state_copy` | Badge when the platform is unavailable and freshness cannot be checked | — |
| `report_state_skipped` | Badge for a report that has no copy at all (`mirrorable: false`) | — |
| `library_skipped_head` | Heading of the section with such reports under the main list | — |
| `library_count` | Counter in the panel above the list, on the right | `{p1}` — how many reports |
| `column_report`, `column_state`, `column_updated` | Table header. The fourth column (actions) has no heading — as on the platform | — |
| `action_open_local` | The "open the local version" link under the report row. A separate text and not a repeat of `report_local_only`: the same phrase twice in a row did not read as a button | `{p1}` — date and time of the run |

## Update from folders and the catch-up queue

The tone rule here is special: a person presses the button when the platform
is down and the numbers are needed now. A refusal must say whether it will
pass by itself (the rights term, a calculation part not yet downloaded) or
never will (a warehouse instead of folders).

| Key | When | Parameters |
|---|---|---|
| `action_update_from_folders` | A button on the card of a report that is allowed a local update (`update.can` in `data.json`) | — |
| `local_run_running` | The run has started, the stage is not named yet | — |
| `local_run_stage` | A stage is going, no number yet | `{p1}` — the stage or its message |
| `local_run_progress` | A stage with a count is going | `{p1}`, `{p2}` — done and total |
| `local_run_done` | The run ended in success; it also says that it is not yet published to the platform | — |
| `local_run_failed` | The run did not start or did not finish | `{p1}` — the refusal caption (see below) |
| `local_run_err_*` | The caption for a refusal CODE of `localrun.precheck`: `busy`, `rls`, `no_right`, `rights_expired`, `no_bundle`, `no_generator`, `gp_bundle`, `no_agent`. For a code that is not here (the `folders.ini` refusals) the page shows the agent's own words — a second wording must not be invented, they would diverge from what the run says | — |
| `local_only_no_bridge` | The run has `bridge: "none"`: the data for such a report is fed by the platform, and locally it will show empty sections. We say so BEFORE the person sees it | — |
| `catchup_title` | Heading of the inset about an unpublished run | — |
| `catchup_waiting` | There is a run, the platform has not answered yet: there is nowhere to publish, and asking about it would be mockery | `{p1}` — date of the local version |
| `catchup_question` | The sync PASSED AGAIN — only now do we ask "publish?" (owner's decision: ask, do not publish silently) | `{p1}` — date of the local version |
| `catchup_how` | How exactly it is published: from the report page, and the version note will carry both dates | — |
| `action_publish_on_platform` | The button in the catch-up inset; leads to the report page in the browser | — |
| `action_open_folders_ui` | The road out of the refusal "folders not chosen yet" (`folders_*` from `agent/runs.py`): leads to the "Report folders" screen (`agent/foldersui.py`), where a folder is chosen with the “Browse…” button. A refusal that names no road once cost the owner a working day | — |

Since 20.10.2026 the same keys work in the viewer HEADER too: the “Update from
folders” button stands next to “Copy from …”, because the question "the
numbers are yesterday's, where is the button" is asked exactly there, inside
the report. There is one difference from the list and it is deliberate: in the
list a forbidden update has NO button, while in the header it is there and
muted — pressing it expands a bar with the reason (`local_run_err_*` or the
agent's words) and, where a road exists, with that road's button
(`action_open_folders_ui` for `folders_*` refusals, `action_sync_now` for
`rights_expired`/`no_bundle`). A silent button is worse than a missing one —
but worse than both is a person who does not know where to go.

## Why a report is not in the copies

Shown instead of the state for a report with `mirrorable: false`. The report is
NOT removed from the list (RFC §4 decision): a person must see the reason and
not decide the report was lost. The string is chosen by `skip_reason` from the
manifest.

| Key | `skip_reason` | Parameters |
|---|---|---|
| `report_skipped_rls` | `"rls"`. The words "RLS" and "row-level security" are deliberately absent from the text: a person must understand it is a property of the report, not a breakage and not their fault. The `action_open_on_platform` button next to it is appropriate | — |
| `report_skipped_draft` | `"draft"` — the report is not published | — |

If an unknown code arrives, `report_skipped_draft` must not be shown — that
would be a lie; it is better to show nothing and write the code to the log.

## Local update is blocked

| Key | When | Parameters |
|---|---|---|
| `local_update_blocked_rights` | More than `rights_ttl_s` (a day) has passed since the last successful sync. Only data updating is blocked, reading never is (RFC §5), and the line says so explicitly | `{p1}` — date and time of the last successful sync |

## Empty library

| Key | When | Parameters |
|---|---|---|
| `library_empty_title` | Heading of the empty state: not a single copy on disk | — |
| `library_empty_hint` | Under the heading, when there is no key or the sync has not been started yet. Next to it is the `action_add_key` button | — |
| `library_empty_no_access` | The sync passed, the manifest returned an empty `items`. It is mandatory to tell this from the previous one: the advice "add a key" to a person who has a key sounds like mockery | — |

## Key hint

The dialog for adding a key (`action_add_key` / `action_replace_key`). The
three strings go as paragraphs in this order.

| Key | What it says | Parameters |
|---|---|---|
| `key_hint_where` | Where to get it. Names the profile item “Key for the desktop program” — **this item does not exist on the platform yet** (there are only the `/api/me/device-tokens` handlers); when it is made, the item name must be checked and corrected here if needed | — |
| `key_hint_once` | The key is shown once; what to do if it was not copied | — |
| `key_hint_scope` | The key is read-only and has a term — removes the fear "I gave the program access to everything" | `{p1}` — term in days, take from `ttl_days` of the `GET /api/me/device-tokens` response or 90 |
| `key_added` | Toast after the key is saved in `creds.py` | — |

## Buttons

| Key | Where |
|---|---|
| `action_sync_now` | The button in the status line and in the tray menu |
| `action_retry` | Next to `sync_server_unavailable`, `sync_no_disk_space` |
| `action_add_key` | Next to `sync_no_key`, in the empty library |
| `action_replace_key` | Next to `sync_key_expired`, `sync_key_rejected` |
| `action_open_report` | On the card of a report with a copy |
| `action_open_on_platform` | On the card of a report without a copy (RLS, draft); leads to the browser |
| `action_show_log` | Tray menu; opens `op-agent.log` (see `agent/applog.py`) |

## Relative time

For `{p1}` in `sync_done` and for `sync_server_unavailable`. Older than a day —
show the date `DD.MM.YYYY HH:MM`; relative "d ago" quickly loses meaning
after a few days of idle time.

| Key | Parameters |
|---|---|
| `time_just_now` | — |
| `time_minutes_ago` | `{p1}` — minutes |
| `time_hours_ago` | `{p1}` — hours |
| `time_days_ago` | `{p1}` — days (1–3) |
| `time_in_minutes` | `{p1}` — minutes until the next attempt |

There is no declension on purpose: the abbreviated "min", "h", "d" do not
decline.
