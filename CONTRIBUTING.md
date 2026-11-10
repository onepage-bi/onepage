# Contributing to OnePage

Thanks for your interest. Please read the first section: the way changes get
into this repository is unusual, and you should know it before you invest time.

## How this repository is maintained (read this first)

Day-to-day development happens in a **private repository**. This public
repository is a **release
slice** of it, produced by `deploy/public-slice.sh` and checked by
`tools/public_sanitize.py`. Releases arrive here as a whole; day-to-day
commits do not.

What that means for you:

- An external pull request cannot be merged here directly. If a maintainer
  accepts it, the change is **cherry-picked into the private repository with
  your authorship preserved**, and it comes back to this repository with the
  **next release**. Expect a delay between "accepted" and "visible on `main`".
- Issues are welcome and are the best way to start: describe the problem
  before writing a large patch, so the work is not wasted.
- Commit history here is therefore not a faithful record of day-to-day
  development.

If this process does not suit you, forking under the Apache 2.0 license is
completely fine.

## Running the project

See [README.md](README.md) for setup. In short: `make dev` starts it in Docker
at http://localhost:8000, and `make venv && make run` runs it without Docker.

## Tests

```
make test              # python smokes, node DOM tests, i18n gate
python3 tests/run_all.py
```

`make test` needs Node.js. Please run the tests before opening a pull request
and say in the description what you ran. The pull request template has a
checklist.

Some browser-engine features need vendored assets (`make assets`, about 105 MB,
needs internet). Chrome-based tests run nightly (`.github/workflows/chrome.yml`)
and are currently allowed to fail; do not treat a red nightly run as caused by
your change without looking.

## Lint: ruff is deliberately narrow

`ruff check .` runs with a minimal rule set (see `pyproject.toml`): only syntax
errors and undefined names. There is no formatter and no style rules. This is
on purpose: a mass reformat would destroy `git blame`, which is the main way
to understand why old code looks the way it does. Please:

- do not reformat code you are not otherwise changing;
- do not send "style only" pull requests;
- match the style of the file you are editing.

## Language of comments

Code comments, docstrings and documents are written in **English**. The
project started in Russian and was translated in full for the public release;
if you meet a leftover Russian comment, translating it is a welcome fix, but
keep it out of unrelated functional changes.

## User-facing text and i18n

The interface supports `en`, `ru` and `zh-HK`. The **translation key is the
English source string itself**, wrapped in the translation call (`_()` in
Python and Jinja, `t()` in JS); there are no symbolic IDs, and English needs
no catalogue. Consequences:

- Write every UI string in English, inside a wrapped call, as a whole sentence
  with `{parameters}` — never assembled from fragments. Plural keys carry the
  two English forms: `tn(n, '{n} file|{n} files')`.
- Run `python3 tools/i18n_extract.py` to register the key, then add the
  Russian and zh-HK translations to `app/locales/{ru,zh-HK}.json` (Russian
  plurals have three forms, zh-HK one) and `python3 tools/i18n_merge.py
  --client` to rebuild the browser bundles. A missing translation is not an
  error: the English source shows until someone translates it.
- `tools/i18n_check.py` is the gate (it also runs in `make test`). It fails on
  Cyrillic anywhere in interface code, unknown or obsolete keys, and
  malformed placeholders or plural forms. Where Russian is data rather than
  interface (the Russian-language question parser, anonymiser fakes), the
  exceptions live in `tools/i18n_allow.txt`, each with a reason.

Course content under `/learn` lives in `app/content/learn.<course>.<lang>.json`;
English is the source, Russian a language pack.

## Data boundary

Nothing from real exports may enter this repository: no customer or company
data, no personal data, no internal hostnames, no secrets, in code, fixtures,
tests, documents or screenshots. Use synthetic data (see `tools/seed_demo.py`
and the screenshots under `static/img/docs/`, taken on a synthetic report).

`tools/public_sanitize.py` is the gate:

```
python3 tools/public_sanitize.py --check .
```

It matches classes of leaks by shape, not a fixed list of strings. Allowing an
exception goes through `tools/public_allow.txt` with a stated reason. If you
are unsure whether something is safe, leave it out and ask in the issue.

## Pull request checklist

- Tests pass (or the failure is named and explained).
- `ruff check .` is clean.
- `python3 tools/public_sanitize.py --check .` is clean.
- New behaviour has a test, or the description says why not.
- You have the right to submit the change under the project license.

By contributing you agree that your contribution is licensed under the
Apache License 2.0, the license of this project.

## Conduct and security

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
Vulnerabilities go through the private channel described in
[SECURITY.md](SECURITY.md), not public issues.
