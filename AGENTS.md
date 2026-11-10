# AGENTS.md — for the coding agent a *user* points at this repository

This file is for Claude Code, Cursor, Copilot and similar tools when a person
asks them to **install OnePage** or **write a report for it**. It is not the
developer context for working on OnePage itself — that is `CLAUDE.md`.

## Install it for me

1. Check Docker. With Docker: `git clone https://github.com/onepage-bi/onepage && cd onepage && make demo`.
   The first run builds the image; the app answers at http://localhost:8000.
   Without Docker: `make venv && make run` — needs Python 3.12 or 3.13
   (3.14 breaks the pinned SQLAlchemy; `make venv` refuses it and says so).
2. Sign in at http://localhost:8000/dev-login as `admin` (development bypass,
   enabled by `OP_DEV_BYPASS_AUTH=true`, which `make dev`/`make demo` set).
3. `make demo` seeds three accounts (`admin`, `analyst`, `viewer`), two demo
   reports and a navigator page. Everything in it is synthetic.
4. Only if the person wants in-browser generators or the /explore engine:
   `make assets` (downloads ~105 MB; needs internet once).
5. Verify: `curl -s localhost:8000/health` returns `{"status":"ok",…}`.
   Tests: `make test`.

Production sign-in is OIDC, SAML or password hashes in `OP_LOCAL_USERS`
(`python -m app.auth.local hash`). See `.env.example` — its comments sit
ABOVE each variable on purpose; an inline comment would become the value.

## Write a report for OnePage

Load the `onepage-report` skill (`.claude/skills/onepage-report/SKILL.md` —
copy that folder into any project's `.claude/skills/` to use it outside this
repository), or give the model the prompt block in `REPORT_GUIDE.md`,
section 13, verbatim. Both carry every hard rule in one place. The five that break a report outright:

- no `localStorage` (the sandbox throws `SecurityError`);
- no `fetch` to APIs and no external resources (CDNs, Google Fonts) — the
  platform is designed for isolated corporate networks;
- data goes in a slot file, not inline in the HTML;
- after `load`, post `op_ready` to the parent, or data never arrives;
- fluid layout from 560 px, because the chat pane takes width.

Then upload the single HTML file at `/upload`. A report that reads a data file
is type `interactive`; one with the numbers inside is `snapshot`.

## Things not to do

- Do not add `allow-same-origin` to the report iframe or weaken the sandbox.
- Do not put real data into fixtures, screenshots or tests — `tools/public_sanitize.py --check .` must stay clean.
- Do not commit the vendored engine assets (`static/pyodide`, `static/duckdb-wasm`, …); they are downloaded by `make assets`.
