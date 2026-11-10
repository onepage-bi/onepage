# Model providers for plain-language "Ask" (/explore)

The "Ask" button on `/explore` can reach a language model by two roads. This
file is the single place that states **exactly what leaves the company**,
**who holds the key** and **which environment variables turn it on**.

One rule does not change on either road: **cell values never go to the
model**. This is not a promise in prose but a property of the code — the
prompt is built by `nl2sql.build_intent_prompt` from definitions
(`semantics.json`), and the tests in `tests/smoke_explore_intent.py` fail if
even one value from the data reaches the prompt or the
`/api/explore/intent/prepare` response.

The tier 1 prompt consists of: one line per KPI (name, definition, up to 6
`question_forms`, list of dimensions), a "Dimensions (what each one means)"
block — the dimension name plus that column's `description` from the
semantics (first 90 characters, author's prose only), and a "Terms" block
(glossary, ≤ 24 lines). Not a single line comes from the data.

## The three tiers of "Ask"

A question goes through the tiers from top to bottom and stops at the first
one that can answer:

| Tier | What happens | Who writes the SQL | Is the answer run |
|---|---|---|---|
| 1. Metric | `match_forms` / the model picks a KPI from `semantics.json`, the engine builds SQL from the frozen `cell_sql` | **the engine** (deterministically) | yes, immediately |
| 2. Report semantics | no KPI fits: the model writes one SELECT over the report's views and tables | **the model** | yes, with a visible label |
| 3. Bare catalogue | the folder has no semantics at all: `POST /api/explore/nl2sql` over raw catalogue tables | **the model** | no, editor only |

Tier 1 is "one-shot": if the question matched a metric's phrasing
(`match_forms`), the metric is chosen **deterministically**, and the model is
called again — this time with `fixed_kpi` — only for the cut
(dims/filters/order/limit). The model cannot swap the metric:
`build_intent_schema(..., fixed_kpi=…)` leaves a single key in the enum with
no `null`, and `parse_intent(..., fixed_kpi=…)` sets it itself whatever the
model wrote. If there is no model at all (neither on the server nor in the
browser), the page computes the **grand total for the metric** (dims=[]) and
opens the metric's form — there is an answer straight away, and the cut is
refined with one click.

### "Why" questions — a separate class, before all tiers

`nl2sql.classify_question` analyses the question **before** any prompt is
built: a question beginning with why / what for / because of what / what
happened (or with "why" among the first three words) is class `why`. (The
classifier matches the Russian forms of these phrases.) The reason was a
defect: "why did SL drop in January 2026?" went to tier 1, the model picked a
trend KPI, `ground_intent` set the year and month, and the superlative rule
added `ORDER BY value DESC LIMIT 1` to a one-row aggregate — SQL that runs
and answers nothing.

Now such a question **never** reaches the ordinary intent prompt. The
keywords are stripped (`strip_why`), the remainder goes to `match_forms`; if
the metric was not settled deterministically, the model is called with a
separate prompt (`build_why_prompt`) — it is asked **only for the metric
key**, and `why_intent_from_raw` throws away everything else. The response of
the `/intent`, `/intent/prepare` and `/intent/validate` routes is
`"source": "why"` plus `"why": {"period", "kpi"}`; the intent has no `dims`,
no `order` and no `limit`. The word "why" does not reach any prompt (checked
by a test).

From this the page (`whyCard` in `static/js/explore.js`) assembles a
**card**: the metric for the named period, the same metric for the previous
period (`shift_period` / `shiftPeriod` — identical rules on the server and on
the page: January → December of the previous year, week 1 → week 52 of the
previous year, day → the previous calendar date) and the top 3 for each
non-time cut of the metric. All sub-queries are deterministic `intent_sql`,
run through the worker strictly one after another; a failed sub-query does
not cancel the card, it is named in the status line.

Tier 3 is reachable only when the connected folder has **no** ready
semantics: guessing from raw 1C headers is worse than honestly saying that
tiers 1–2 did not answer.

## What leaves the company

With an external provider (DeepSeek), the following leaves the company:

- **the user's question** — as they wrote it;
- **tier 1** — names, definitions, question phrasings and cuts (dims) of the
  metrics from the `semantics.json` of the generator being asked about, and
  only those metrics that remained in the shortlist (`rank_kpis`, up to 6;
  with `fixed_kpi`, exactly one);
- **tier 2** — **names and descriptions** of the report's tables and views:
  the relation name, its description and granularity from `semantics.json`,
  column names, their types and the author's descriptions (written in
  Russian in the existing bundles), and the text of the rules (`rules`). Only
  relations that were actually built on the connected folder are described
  (`bindings_status` from the worker);
- **glossary terms** — platform (`glossary_terms`) and bundle terms, up to 24,
  160 characters each;
- **tier 3** (`POST /api/explore/nl2sql`) — the schema of raw catalogue
  tables: table and column names, types, partition keys (and, only if the
  operator explicitly raised `OP_LLM_EXAMPLE_VALUES`, up to 3 example values
  per column — 0 by default).

What does **not** leave: data rows, cell contents, the metrics' own SQL
(`cell_sql` stays on the server and in the browser), user names, files. This
also holds for tier 2: `build_semantic_sql_prompt` builds the prompt from
`semantics.schema_outline`, which determines the views' columns by running
their SQL over **empty** typed tables in an isolated DuckDB — not a single
data row exists on that path. A test pins this separately: even the `rows` of
an inline table (values stored directly in `semantics.json`) do not reach the
tier 2 prompt.

### "Chat" (phase 3): history stays in the browser

A "Chat" thread on `/explore` is IndexedDB in **this browser** (`op-explore`
/ `history`, key `${user}:${report}:${n}`, up to 500 records per report, up
to 200 rows per record). No result row, no column and no thread record is
sent anywhere: `static/js/explore-chat.js` adds no new endpoint, and its only
POST is the pre-existing "Pin" (`POST /api/explore/questions`, which receives
a title and the SQL text, as before).

Follow-up questions ("and by week?") are answered **on the client**, with no
server request and no model: the metric has already been chosen by the open
answer, and the question changes only the cut or the period. When the client
matcher cannot cope, exactly **one** `prev_intent` may leave — the intent of
the answer the person is currently looking at
(`kpi/dims/filters/order/limit`, validated on the server). The thread as a
whole is never sent. Only **one line** of it reaches the prompt —
`nl2sql.prev_intent_line`: the metric name, the names of the cuts and the
names of the time dimensions that set the period. **There are no values in
it**: `month = 7` is data read from the user's folder, and it does not cross
the data boundary. The previous period is carried into the new answer
deterministically, on our side (`nl2sql.ground_intent(..., prev_intent=)`).

The model's answer on tier 1 is not SQL but a filled-in form (`{kpi, dims,
filters, order, limit}`), and it is validated on the server
(`parse_intent` + `ground_intent`) against the frozen semantics: the model
cannot invent a metric, cut or operation that is not in the definitions — the
validator throws them out.

On tier 2 the model writes SQL, so there are two checks, both fail-closed:
`nl2sql.is_select_only` (one SELECT, nothing after a semicolon, no
DDL/DML/file reads) and **the worker's very own `guard_sql`**
(`static/py/_explore.py`, imported via `semantics._worker_guard()` — not
rewritten a second time). Whatever passes here, the engine will accept;
whatever does not pass never reaches the page. In the browser the same
`guard_sql` runs once more, right before a read-only DuckDB. That is why
tier 2 SQL **is executed**, but shown with the label **"SQL was written by
the model from the report's semantics — check it"**: the answer matters more
than an extra click, and safety rests on the guard, not on the click.

## The intent verifier (ADR-15)

After the intent is assembled and before the number is shown, the page may
make ONE advisory model call: "does the assembled query answer the question,
is the format right". What leaves the company in that call: the user's
question, the name/definition of the chosen metric, the names of the cuts,
the filters (values taken from the question text), the output format. What
does not leave: cells, rows, results, SQL. A "does not answer" verdict holds
back the number and shows the verifier's clarifying question (with a
"calculate anyway" button); any error/timeout is fail-open, and the answer is
shown as usual.

## Data on the server (ADR-14/14b)

The model is not the only recipient of data worth stating explicitly. Since
ADR-14 the server STORES the report's source data: Parquet partitions of the
source folder, published by the report's run to
`data/uploads/_lake/{dashboard_id}/`. Since ADR-14b the server also COMPUTES
over them: `POST …/lake/exec` runs /explore queries with the native engine
(`app/services/lake_exec.py` on top of the same `static/py/_explore.py`), and
the answer rows travel server → browser.

Boundaries:
- anyone with access to the report (`require_dashboard_access`) may read
  (download partitions, run queries) — exactly like `/d/{id}`;
- whoever can run the generator may write; only the latest run is kept;
- cells still do not go to the MODEL: `lake/exec` talks to no LLM at all, and
  intent prompts are built as described above;
- share links (`/s/{token}`) have no access to lake or exec;
- SQL is not written to the audit log: filter literals may quote data values;
  the operation and its duration are audited.

Switches: `OP_LAKE_EXEC=false` moves execution back to the browser
(partition download); limits are `OP_LAKE_PART_MAX_MB`, `OP_LAKE_MAX_MB`,
`OP_LAKE_EXEC_INSTANCES`.

## Reference tables on the server (ADR-16)

**What is stored.** Platform reference tables (`reference_tables`) are
key-value tables (1:1 or many-to-one mappings). The server holds: the
reference rows (up to 20k per table, up to 2 MB), all versions (delta
history: added/changed/removed), the responsible person, and an audit log
(created/updated).

**Who reads.** An admin, or a user who has access to any report that consumes
this reference table (`require_dashboard_access`, as for `/d/{id}`).

**Who writes.** The person responsible for the reference table; if nobody
is, anyone with `can_update_data` on a consumer; otherwise an admin.

**How it is created.** From Excel (the schema is inferred or declared by the
manifest); column types are frozen. A version is created on **every** import;
the `allow_pin` checkbox only allows choosing a version when the report is
run (a run parameter).

**What leaves the company during a run.** Two things:
- **misses** (`missing`, ≤500 unique + overflow) — raw codes from the data
  that were not found in the reference table; example: 12 new NWD codes in
  the SL report. They are sent as `{table, values, more, responsible}` in
  `run_summary` to show the banner "Reference table X: N new values — owner
  Y" after the run;
- **bypassing the 500 ceiling** (`known_values` checks): `ref_lookup` returns
  only the misses, not the whole set of known values.

**What does not go to the model.** Reference rows, mappings — nothing. The
model sees only the names of reference tables and their purpose (`ref_lookup`
is an input-check type), never their contents. The check result (`missing`)
goes to Data Quality, but **not into** `/explore` **prompts**.

**What is not exposed to the browser.** Nothing — a reference table is
edited only by someone with `can_update_data`; everyone else sees only the
results (misses in the report and on the reference table's page).

**Versions and rollback.** A version is an immutable row (created on import).
A rollback = "make version N current", which creates a **new** version (a
copy of the old one). History is linear.

**Limits.** Key ≤64 characters (normalisation: trim + collapse whitespace
incl. nbsp + unify dashes + casefold). The metadata fingerprint (`sha256`)
and the content fingerprint (`sha256` of all rows) stay on the server and are
not transmitted. Share links (`/s/{token}`) have no access to reference
tables.

**Check in the engine.** `ref_lookup` runs even when the "Apply reference
table" checkbox is off — the owner still needs to know about misses. When it
is on: the reference table is taken by version, keys are normalised (same
rule), and the comparison is normalised. When it is off: the check looks for
misses against the current version.

## Road 1: the server calls the model

```
browser → POST /api/explore/intent → server → provider
```

This is how the historical path with Ollama works, and DeepSeek works the
same way if the server has internet access. The implementation is
`nl2sql.call_llm`, which, depending on `OP_LLM_PROVIDER`, dispatches to
`call_ollama` (`POST {endpoint}/api/generate`) or `call_deepseek`
(`POST {endpoint}/chat/completions`, header `Authorization: Bearer …`,
`temperature: 0`, `max_tokens: 400`,
`response_format: {"type": "json_object"}`).

**Measured in production (2026-08-22): the OnePage container has no
egress** — `httpx.get('https://api.deepseek.com/models')` ends in
`ConnectTimeout`. So this road is not used in production; it remains for
Ollama and for running `tools/intent_eval.py` from a laptop that does have
egress.

## Road 2: the user's browser calls the model (the production path)

```
browser → POST /api/explore/intent/prepare  → server (prompt, no model)
browser → POST {endpoint}/chat/completions  → provider (the key is sent from here)
browser → POST /api/explore/intent/validate → server (answer validation)
```

Users' browsers do have egress — through the corporate proxy. The server
meanwhile keeps doing everything except the call itself: the permission
check (access to the generator), semantics, form matching, the prompt, the
schema and — most importantly — validating what the model returned. Text
coming from the browser is not trusted: it is the same "model speech" as an
Ollama answer and goes through the same `parse_intent`.

If the question confidently matched a metric's phrasing (`match_forms`),
`prepare` responds with a ready intent and **the page calls nobody at all** —
nothing leaves the company.

`GET /api/explore/llm-config` tells the page `{provider, endpoint, model,
browser, key}` and deliberately **requires neither `OP_LLM_ENDPOINT` nor
`OP_LLM_API_KEY` on the server**: a deployment without egress sets only
`OP_LLM_BROWSER` and `OP_LLM_BROWSER_KEY`.

### CORS

Verified from this machine (`curl -X OPTIONS https://api.deepseek.com/chat/completions
-H 'Origin: https://onepage.example.com' -H 'Access-Control-Request-Method: POST'
-H 'Access-Control-Request-Headers: authorization,content-type'`):

```
HTTP/2 200
access-control-allow-origin: https://onepage.example.com
access-control-allow-methods: POST
access-control-allow-headers: authorization,content-type
```

So DeepSeek sends CORS headers and answers the preflight — calling it from
the page is possible. The same was confirmed from a corporate browser on a
OnePage tab: `fetch('https://api.deepseek.com/models')` returned status 401
(not the `TypeError` that a call without CORS would end in).

If this stops working for some deployment (the provider dropped the headers,
the proxy cuts the request, DNS does not resolve), `fetch` in the browser
fails with `TypeError` — the page says **"The browser couldn't reach
api.deepseek.com: … — trying via the server…"** and tries the server road.
The path never breaks silently.

## Keys

| Where it lives | Variable / storage | Who gets it |
|---|---|---|
| Server, for its own calls | `OP_LLM_API_KEY` | nobody: only the header of the outgoing request |
| Server, to hand out to the page | `OP_LLM_BROWSER_KEY` | a signed-in user who can run at least one generator |
| User's browser (BYOK) | `localStorage['op-llm-key']` | only this browser; never sent to the OnePage server |

- The key is **not logged**: `OP_LLM_LOG` writes the prompt, not the headers,
  and the provider's error text is additionally scrubbed (`nl2sql._scrub`).
- Handing out `OP_LLM_BROWSER_KEY` is written to the audit log **once per
  session** (`explore_llm_key`) — the record does not contain the key itself,
  only the fact of the hand-out, the provider, the model and the endpoint.
- If `OP_LLM_BROWSER_KEY` is empty, the page shows a **"DeepSeek key (stored
  only in this browser)"** field: the user enters their own key (BYOK), it is
  stored in `localStorage` and sent only to the provider.
- Every `/api/explore/intent/validate` call is written to the audit log with
  `"via": "browser"` — you can see who asked and what the model chose.

### Key rotation

1. Create a new key in the DeepSeek console.
2. Change `OP_LLM_BROWSER_KEY` (and/or `OP_LLM_API_KEY`) in `.env` and
   recreate the container.
3. Revoke the old key at the provider.

There is no need to reload the tab: the page re-reads `llm-config` before
every question, so the new key arrives with the next question. BYOK keys are
rotated by the users themselves — clear the "DeepSeek key" field.

## Admin card: switching provider without a redeploy

`/admin` provides an "enable DeepSeek" switch that does not require
rebuilding the container. The `OP_LLM_*` variables remain the **default**
layer; what the administrator saves in the card is stored as an overlay in
`data/llm-runtime.json` (`app/services/runtime_config.py`) and applied on top
of `settings` — `env` does not go anywhere: unticking a setting in the card
restores exactly the value from `env`, not the last one applied. The overlay
is re-read at most once per second (by the file's mtime), so several workers
(`OP_WORKERS > 1`) converge to one state without inter-process signalling,
and restarting the container does not reset the administrator's choice —
`llm-runtime.json` lives in `data/`, the volume that survives container
re-creation.

The `llm_api_key` and `llm_browser_key` fields are **write-only**: `PUT
/api/admin/llm` accepts them, but `GET /api/admin/llm` never returns the
value, only `{name}_set: true/false`; the file on disk has `0600`
permissions; the audit log entry (`llm_config_update`) stores only the
**names** of the changed fields, not the values — the same discipline as the
generators' git token. Both endpoints require `manage_users`.

## Who can use DeepSeek

The paid provider is available by list, not by role. The `llm_access` table
holds one row per user who may use it; for Ollama (local, free) the list is
not checked at all.

- **Who can USE it**: while `OP_LLM_PROVIDER` = `ollama`, everyone has
  access. As soon as the provider is `deepseek`,
  `llm_access.allowed(db, user)` requires a row in `llm_access`; without it
  the server does not call the model, does not prepare a prompt for the
  browser and does not hand out `OP_LLM_BROWSER_KEY` — deterministic answers
  (form, "why", a chosen metric) keep working for **everyone**, because the
  check sits only where the model call itself or the key hand-out happens
  (`/nl2sql`, both branches of `/intent`, `/intent/prepare`,
  `/semantic-sql/prepare`, `/semantic-sql`, and inside `/llm-config` —
  without an error, simply `server`/`browser` become `false` and `key`
  becomes `null`). The refusal is a **403** with the text (English
  catalogue): `"DeepSeek model is available to a limited group – contact
  administrator"`.
- **Who can ASSIGN it**: an administrator (`manage_users`), further narrowed
  by the `OP_LLM_ACCESS_ADMINS` variable (comma-separated person_ids) — it is
  read only from `env` and is never written to the DB or to
  `llm-runtime.json`, so only someone with access to the server, not any
  administrator through the UI, can widen the list of assigners. A refusal on
  `PUT`/`DELETE /api/admin/llm/access/{person_id}` is a **403** with the text
  (English catalogue): `"DeepSeek access is granted by designated admin
  only"`.

The card on `/admin` (`static/js/admin-llm.js`) lists/grants/revokes access;
the input field is a full-name typeahead on `data-user-suggest`
(`static/js/user-suggest.js`, the same widget as in the employees
directory), not separate code.

## Where to look when "nothing works for me"

On the production path, everything that can break after the server has
answered breaks **in the browser**: the DeepSeek call through the corporate
proxy, executing the model's SQL in DuckDB-WASM, the page's JS. The server
knows nothing about it, and `docker logs` is empty by default
(`OP_ACCESS_LOG=false`; the application writes to the `audit_log` table, not
to stdout).

So since 2026-08-28 the page reports every failure itself —
`POST /api/explore/client-error` → an `explore_client_error` row in
`audit_log`. In `detail`: `stage` (where it broke), `reason` (the text the
user saw), `question`, `status` (HTTP), `host` (provider), `tier`, and for the
second tier the model's `sql` that the engine rejected. There are never any
data cells or keys in it.

```bash
cd ~/onepage && sudo docker exec -i onepage-onepage-1 python3 - <<'PY'
import sqlite3, json
db = sqlite3.connect('/data/onepage.db')
for ts, who, det in db.execute("""
  SELECT a.created_at, coalesce(u.display_name, u.person_id), a.detail FROM audit_log a
  LEFT JOIN users u ON u.id = a.actor_id
  WHERE a.action = 'explore_client_error' ORDER BY a.id DESC LIMIT 30"""):
    d = json.loads(det or '{}')
    print(ts, who, d.get('stage'), d.get('status'), '|', d.get('reason', '')[:80], '|', d.get('question', '')[:50])
PY
```

How to read `stage`:

| stage | where | what to do |
|---|---|---|
| `provider_http` 401/402 | browser → DeepSeek | key rejected — the user enters their key again |
| `provider_http` 429 / 5xx | DeepSeek | provider rate limit/outage, retry later |
| `provider_network` | proxy | the browser could not reach `host` — the proxy blocks the domain; the page tries the server |
| `provider_empty` | DeepSeek | empty model response |
| `intent_validate` / `sql_validate` | server | the model's answer failed validation (`reason` = the server's detail) |
| `query_failed` | DuckDB in the browser | the SQL (in `sql`) failed — on the second tier this is the model's SQL |
| `no_model` | browser | no key and no model on the server — the question did not match a metric form |
| `chat_no_answer` | chat | "umbrella": a send with no answer; `reason` is what the user was shown |

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `OP_LLM_PROVIDER` | `ollama` | `ollama` \| `deepseek` |
| `OP_LLM_ENDPOINT` | `""` | provider base URL; for `deepseek`, empty = `https://api.deepseek.com` |
| `OP_LLM_MODEL` | `qwen2.5-coder:7b` | for `deepseek`, unset = `deepseek-chat` |
| `OP_LLM_API_KEY` | `""` | bearer key for calls **from the server** |
| `OP_LLM_TIMEOUT_S` | `20` | model call timeout |
| `OP_LLM_STRUCTURED` | `true` | Ollama: `format`; DeepSeek: JSON mode + schema as text in the prompt |
| `OP_LLM_BROWSER` | `false` | allow the page to call the provider itself |
| `OP_LLM_BROWSER_KEY` | `""` | key the server hands out to the page; empty = BYOK |
| `OP_LLM_EXAMPLE_VALUES` | `0` | example values in the schema prompt (`/nl2sql`); 0 = schema only |
| `OP_LLM_LOG` | `false` | log the prompt (not the key) |
| `OP_LLM_ACCESS_ADMINS` | `""` | comma-separated person_ids — who may assign DeepSeek access; empty = any administrator; **env-only**, never written to `llm-runtime.json` |

Six of these — `PROVIDER`, `BROWSER`, `MODEL`, `ENDPOINT`, `API_KEY`,
`BROWSER_KEY` — an administrator can override on `/admin` without a redeploy
(`OVERRIDE_FIELDS` in `runtime_config.py`) — see "Admin card" above;
`TIMEOUT_S`, `STRUCTURED`, `EXAMPLE_VALUES`, `LOG` and `ACCESS_ADMINS` are not
part of that overlay and can only be changed via `.env` + re-creating the
container.

Production (server without egress, browsers through a proxy):

```
OP_LLM_PROVIDER=deepseek
OP_LLM_BROWSER=true
OP_LLM_BROWSER_KEY=sk-...
# OP_LLM_ENDPOINT / OP_LLM_MODEL / OP_LLM_API_KEY are not needed
```

## How this is tested

- `tests/smoke_explore_intent.py` — the shape of the DeepSeek request
  (bearer, `json_object`, system+user, `temperature 0`), 503 on any refusal
  with no key in the error text, `prepare` (form without a model / prompt
  without values), `validate` (good JSON, and 422 on garbage), the key and
  the `server` flag handed out in `llm-config`; the `fixed_kpi` contract
  (shortlist and enum narrowed to one metric, the model cannot swap it or
  decline); tier 2 — a prompt with column names, types and descriptions
  **without a single value** (including no inline-table rows), `validate`
  rejects non-SELECT, a second statement after a semicolon, DML after a CTE,
  a file read, and the same file read in quotes.
- `tests/explore_dom.js` — the whole browser road on mocks:
  `llm-config → prepare → /chat/completions → validate`, and the answer **is
  run immediately**; a form without a model computes the metric total and
  opens the form; tier 2 is shown with the label and executed; tier 3 is
  unavailable when semantics are ready; a `TypeError` falls back to the
  server road; the BYOK field appears when there is no server key; table
  grouping in the query builder's drop-down.
- `tests/chrome/smoke_explore_chrome.py` — real Chrome, three variants of one
  and the same flow:

  ```
  python tests/chrome/smoke_explore_chrome.py                       # server → Ollama (default)
  python tests/chrome/smoke_explore_chrome.py --provider deepseek   # server → DeepSeek
  python tests/chrome/smoke_explore_chrome.py --browser             # page → DeepSeek (production path)
  ```

  The stub answers in all three shapes (form JSON, SQL over semantics, SQL
  over the raw catalogue) and sends CORS headers, so the `--browser` variant
  goes through a real preflight from the page. Each variant checks the
  **one-shot** behaviour: after "Ask", rows appear in the table without a
  single extra click, and, as a separate case, tier 2 with a SELECT over the
  `lines` view.
- `tools/intent_eval.py --provider deepseek` — runs the chain over real
  questions (the key is taken from `OP_LLM_API_KEY` and does not appear in the
  output).

## Answer record

Every successful AI answer on `/explore` writes one row to `explore_answers`
(SQLite, migration in `app/main.py::lifespan` — `CREATE TABLE` via
`Base.metadata.create_all` + indexes `(user_id, created_at)` and
`(generator_id, created_at)` in the shared `CREATE INDEX IF NOT EXISTS`
block). Only SUCCESS is recorded; an error/422/403 writes nothing:

| Write point | tier | via | what it carries |
|---|---|---|---|
| `POST /api/explore/intent` (form/why/model) | `form` \| `intent` | `server` | `intent_json` |
| `POST /api/explore/intent/validate` | `intent` | `browser` | `intent_json` |
| `POST /api/explore/semantic-sql/validate` | `semantic_sql` | `browser` | `sql_sha256` |
| `lake/exec` op `sql` marked `final: true` | `lake` | `server` | `sql_sha256` + `question` from the request body |

`lake/exec` is the only write point where "one answer" is not the same as
"one HTTP request": the page builds the SQL via op `intent`, then EXECUTES it
via one or more `sql` ops (the why-card computes several sub-queries —
current period, previous period, top by cuts — for a single question asked).
So the writing endpoint (`app/api/lake.py`) records a row only when the
client marks the call `final: true` and passes `question` in the body — the
widget (`static/js/explore.js`) sets that flag on exactly ONE call per
answer: `runQuery`, `trendCard`, or the headline (current period) sub-query
of the why-card. Op `intent` (building SQL without running it) and any
unmarked op `sql` (the assembly run, the previous period, the why-card cuts,
the background `grandTotals`) write nothing — otherwise one user question
would produce 2–3 rows with the same `sql_sha256`, and the "how many answers
were given" count would be inflated.

Never stored: the answer's result (rows/numbers) and the SQL text — only its
`sha256`, because a filter literal in SQL written by the model or assembled
from an intent may quote a real data value (the same argument for which
`lake/exec` does not log SQL to `audit_log`). `intent_json` is stored as is —
its filter values come from the text of the user's question (a
metric/dimension/literal the person typed themselves), which is not a data
leak.

`generator_version` + `semantics_sha256` are the version and hash of the
FROZEN semantics (ADR-13) the answer was obtained against: a later edit of
semantics.json does not retroactively rewrite the meaning of an old record.
The write is `db.add()` + `flush()` without a commit of its own
(`app/services/explore_answers.py`); the calling route commits — atomically
with everything else that request already writes. `flush()` is needed for
`answer_id`: every write point returns the row's id in the response body
(`/intent`, `/intent/validate`, `/semantic-sql/validate`, `lake/exec` op
`sql` with `final`), because without it 👍/👎 has nothing to attach to. If
the write fails (the current generator version has no semantics, a flush
error), `answer_id` = `null` and the page keeps the vote locally: recording
an answer never fails the request that produced the answer.

Reading: `GET /api/explore/answers?generator=&limit=` — an ordinary user sees
only their own rows, an administrator (`view_admin`) sees all. There is no
page, only the API.

## Votes and the form-suggestion bank (P4)

`POST /api/explore/answers/{id}/vote` `{vote: "up"|"down"|null}` — 👍/👎 on
YOUR OWN answer (someone else's or a non-existent id is 404, not 403: a 403
would confirm that such an answer exists). Idempotent: the same vote a second
time is a no-op, a different one is a change, `null` removes the vote. The
vote is stored in `explore_answers` itself (`vote`, `voted_at`) — the only
thing in a recorded answer that changes after it is written.

The vote is also the only entry into `explore_form_suggestions` (the
suggestion box for the report's author): there is no separate "suggest a
form" route, so that there is exactly one way to create a suggestion and the
client cannot file one against someone else's or a non-existent answer.
Rules:

* 👎 → a suggestion of kind **B** ("answered wrongly, check the definition");
* an INDEPENDENTLY confirmed 👍 on one normalised question (casefold,
  collapsed whitespace, punctuation dropped) for one generator → a suggestion
  of kind **A** (a `question_forms` candidate). "Independently" means either
  three 👍 from one person on THREE DIFFERENT CALENDAR DAYS, or 👍 from TWO
  DIFFERENT people: three clicks of "Recalculate" in a row within a minute
  are one opinion, not three, and must not become a form candidate. A further
  👍 does not create a second suggestion.

Deduplication is a PARTIAL unique index (generator, question hash, kind)
`WHERE status='new'` plus a check before the insert as a fast path: a full
UNIQUE would prevent a declined question from returning to the queue when
people start asking it again (that is a new signal, not a duplicate), and the
check alone is not enough — two simultaneous 👎 both passed it and each
inserted a row.

The answer record all of this hangs off does its `flush()` in a SAVEPOINT
(`app/services/explore_answers.py`): the page needs `answer_id` in the same
response, and a failed insert must roll back only itself — otherwise the
route's next `commit()` fails and "an answer without an id" turns into a 500.

The queue: `GET /api/explore/form-suggestions?generator=&status=` and
`POST …/{id}/approve|decline`. Access requires reachability of the generator
plus `can_update_dashboard_data` on that generator's dashboard (the same
right as for uploading a bundle and starting a run); an unreachable generator
is a 404. For an RLS report (ADR-18) the queue is open only to a curator with
`see_all` — the same filter as for report cards in the chat: a suggestion
quotes someone else's question and its filters, and a viewer with a narrowed
scope must not read them around the slot files. A decision is taken ONCE and
only from `new`: a repeated (or reversed) approve/decline is a 409. Audit:
`explore_form_suggest` on creation and `explore_form_decide` on decision;
the detail holds only the id, kind, metric key and generator — the question
is not in the shared log. The table itself holds no result row and no SQL
text: the question, the metric key, the intent and `sql_hash`.
