# Report chat — the pane in the viewer

A guide for report authors and administrators: what a person sees, what the
report itself needs for it, and what is (and is not) sent to the server.
Why it is built this way — `docs/PERF_DECISIONS.md` ADR-20; the
"Show in report" protocol — `.claude/skills/dashboard-protocols/SKILL.md`,
section Locate; the rules for votes and the queue — `docs/LLM_PROVIDERS.md`,
"Votes and the form-suggestion bank".

## Who sees the pane, and when

The chat button is drawn only when all the conditions hold
(`app/api/dashboards.py`, `can_discuss`):

* the report has a **generator** (`dashboard.generator_id`) — the chat
  answers from the generator's semantics, and a hand-uploaded HTML has none;
* the person has the right to **update the data** of this dashboard — the
  numbers are discussed by the same people who can recompute them;
* an **RLS report with restricted scope** (ADR-18) gets no chat at all: the
  chat reads the lake, and the lake is not filtered by territory.

None of the three answers costs a query — everything is already on the
dashboard row, and `/d/{id}` stays a hot path.

## How the pane opens

A round button in the bottom-right corner of the report (`#discuss-fab`,
carrying a counter of the questions asked about this report). The pane is a
**grid column**: the report is pushed aside, not covered. What it can do:

* The **grip** on the pane's left edge drags the width within 340…62 % of
  the viewer width; a double-click on the grip resets it to 440px.
* **"Wider"** (and `⌘/Ctrl + Shift + ]`) — half the viewer width.
* **Esc** — close (and return focus to the button), but only if nobody else
  has claimed the key: in present/fullscreen mode and with the cursor in an
  input field the pane leaves Esc alone.
* The report is guaranteed **560px**: if the window cannot fit
  "report + chat + dock", the chat yields. Below **760px** there is nothing
  to give away — the pane becomes a full-width overlay and the grip
  disappears.
* Width, "wider" and open state are **remembered per report** in the
  browser. Opening `/d/{id}` with a remembered open pane costs not a single
  new request: the chat frame is created only after the report has been
  drawn.
* Closing the pane **does not unload** the chat: the index it built stays
  alive for another 10 minutes, so returning to the chat is instant.

The question history lives **only in the browser** (IndexedDB `op-explore`),
500 records per report and 200 result rows per record. "Clear chat" clears
it in this browser; nothing of the conversation remains on the server except
what is described below.

## What an answer card can do

* **Tier badge** — who chose the metric: "exact form" (one of the report's
  question forms matched, the model was not involved), "the form matched
  loosely", "understood by meaning" (the model chose), "SQL from semantics",
  "no answer".
* **"Understood as"** — metric, cut, period, filters. NAMES only: the card
  does not repeat the row the answer was read from.
* **"How it was calculated"** — the metric's definition, the SQL and the
  provenance (partitions, freshness, the definition's owner from the
  semantics).
* **An answer without a number is never an empty card.** Zero rows, or an
  aggregate over zero rows, turn into a question back: drop the period
  filter, drop the cut filter, take another metric, rephrase. If the
  question's period is simply **outside the index coverage**, the card says
  exactly that rather than "empty" (the comparison is made at one
  granularity: monthly partitions say nothing about a question about a
  week).
* **Coverage pill** in the header — the partitions and rows of the index
  already loaded in the browser. It costs no request.
* **👍/👎**, **"Regenerate"**, follow-up suggestions ("and by warehouse?"),
  **"Show in report"**.
* **"⋯"** — new conversation, conversation history, "copy conversation",
  "clear chat". A new conversation **deletes nothing**: the old one stays in
  the history, and the counter on the button counts the questions about the
  report as a whole.

## "Refine"

A button on the card when there is an answer but it may have been the wrong
one. The options are ordered from the most precise to the most general:

1. free text — sent as a follow-up to THIS answer (the metric and the period
   are already known);
2. the alternatives the router itself kept in its shortlist, with a score of
   how close each is to the question;
3. "just not this metric" — the same question with a different metric
   pinned;
4. adjust the period or the cut.

All of this is the same path a typed question takes: there is no separate
"refine mode" on the server.

## "Show in report" — what the report needs

The button finds the place in the report itself where this number is shown,
scrolls to it and highlights it for 2 seconds. It works like this: into
**every** rendered report the platform injects a dormant controller
(`inject_annotate` → `_ANNOTATE_CONTROLLER`), and it has an `op_locate`
command. The report author needs to do **nothing** for this — but it helps
to know what is searched for and where:

* the search runs over the **text** of leaf elements, headings, `th` and
  `caption`;
* a number is compared both as a substring and as a NUMBER — `1 234,6` in
  the report is found from `1234.567` in the answer, "99,99 %" from the chat
  by "100,0%" in a cell (units and currency at the edges are dropped, a
  share is also tried as a percentage);
* **one number is not enough**: "100,0%" sits in a dozen cells. The one
  chosen is the cell whose row and column header mention the context of the
  question — filter values ("Magnit", "W23") and the labels of the answer's
  rows; if the number is in several cells and the context matches none of
  them, the button honestly says "not found in the report" rather than
  highlighting the first one it came across;
* if the number is not there, the metric name and the row labels are
  searched for;
* **`canvas` is not searched**: a value drawn only on a Chart.js/Plotly
  chart will not be found — if you want the button to land on it, keep a
  text label next to the chart (a KPI tile, an axis label, a table under the
  chart);
* a giant report is cut off at 20,000 elements, and it is the "tail" that
  is cut, not a random piece: of two equally good hits, the one higher in
  the document wins.

If nothing was found, the card honestly writes "not found in the report",
and if the report did not answer at all (an old viewer, a torn-down frame) —
"the report did not answer". The button never pretends to have scrolled
somewhere.

## Votes and what happens to them

👍/👎 is put **on your own answer** and means "the answer is right / wrong".
The vote is stored in the answer record (`explore_answers.vote`) and, if the
browser is offline, stays local — the card does not break because of it.

A vote is the **only** way into the suggestions box for the report author:

* 👎 → a suggestion of kind **B**: "a wrong answer was given here, check the
  metric definition";
* 👍 → a suggestion of kind **A** (a candidate for the semantics'
  `question_forms`), but only if confirmed INDEPENDENTLY: one person on three
  different calendar days, or two different people. Three clicks in a row
  within a minute do not make a candidate.

Identical undecided suggestions are not duplicated (a partial unique index
on "generator + question + kind" among `status='new'`); a declined question
may come back to the queue later — that is a new signal, not a duplicate.

**The queue is worked through the API; there is no page for it.** Access —
the same people who can update the data of this generator's dashboard (for
an RLS report — only a curator with full scope):

```
GET  /api/explore/form-suggestions?generator=<id>&status=new|approved|declined|all
POST /api/explore/form-suggestions/<sid>/approve
POST /api/explore/form-suggestions/<sid>/decline
```

A decision is made once and only from `new` (a repeat or a reversal is a
409). An approved suggestion is a TASK for the author: add the question to
`question_forms` in the generator's `semantics.json` and re-upload the
bundle; the application does not edit semantics itself. A suggestion row
holds the question, the metric key, the intent and a **hash** of the SQL —
no result rows and no query text. The general log (`audit_log`) records
`explore_form_suggest` and `explore_form_decide` — only the id, kind, metric
key and generator, without the question text.

## Limits and the privacy boundary

* The pane exists **only inside the viewer** (`/explore?...&mode=chat&embed=1`).
  It has no page of its own, and a share link has no chat at all.
* The chat answers from the **frozen** semantics of the report version and
  from the index built in the browser (or from the lake on the server —
  ADR-14b), so the "freshness" in the card footer is the freshness of the
  report's data, not today's date.
* All that leaves the conversation for the server is what is described
  above: **the fact of the answer** (question, tier, intent, sha256 of the
  SQL, semantics and run version), the vote and — following a vote — a
  suggestion row. The server receives **not a single result cell and not a
  single SQL text** from the chat; "copy conversation" also copies the
  questions and numbers, but not the SQL.
* All that goes from an answer into the report frame is the metric name, one
  formatted value and the labels of the first 8 rows — everything needed for
  the highlight.
* The model (if the path reached it) sees the question and the names of the
  metrics and cuts — but not the values from the data: `month = 7` does not
  get into the prompt.

## How to test

```
node tests/explore_chat_dom.js            # cards, clarify, dedup, conversations
node tests/viewer_chat_dom.js             # pane width, persistence, Esc, lazy frame
node tests/detector_locate_dom.js         # search and highlight inside the report
.venv/bin/python tests/smoke_detector_locate.py
.venv/bin/python tests/smoke_explore_feedback.py
.venv/bin/python tests/chrome/smoke_viewer_chat_chrome.py   # column geometry in Chrome
.venv/bin/python tests/chrome/smoke_explore_chrome.py       # all of /explore, both engines
```

The browser runs are skipped silently (`SKIP`, exit code 0) if Chrome is not
installed. Their interface language is set by `OP_CHROME_LANG` — `ru` by
default; `OP_CHROME_LANG=en-GB .venv/bin/python tests/chrome/smoke_explore_chrome.py`
runs the same scenario against the English catalogue. The browser engine
assets (`static/pyodide`, `static/duckdb-wasm`, `static/duckdb-ext`,
`static/arrow-esm`, `static/xlsx-arrow`) are not in git: in a fresh clone or
worktree they have to be copied in (`cp -R`, not a symlink — `StaticFiles`
answers 404 for a symlink).
