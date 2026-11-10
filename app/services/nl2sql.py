r"""NL -> SQL bridge for `/explore` (Leg 3), over Ollama.

The whole "Security" contract for this module in one sentence: the
prompt this file builds carries a SCHEMA — table names, column names, types,
partition keys, and (only when an operator opts in via
`OP_LLM_EXAMPLE_VALUES`) at most 3 example values per column, all pulled
from the catalog snapshot the caller already computed — and never a row of
data. Default is 0 examples: schema-only, matching the plan's "the LLM gets
only the schema … never values" verbatim. No function here reads
`source_files`, `quality_results` or any other table that could hand it a
raw cell; it only ever sees what `app/api/explore.py` already trimmed to
≤500 values ≤64 chars each, and then trims further to `max_examples`.

`build_prompt()` is the piece pinned by `tests/smoke_explore_api.py`: given a
catalog snapshot, assert no value beyond the first `max_examples` examples of
each column ever appears in the rendered prompt (and that, with the default
`max_examples=0`, NO value appears at all). Keeping the prompt builder a
pure string function (no I/O) is what makes that assertion possible without
mocking a network call.

`call_llm()` is the only function the rest of the app calls for a model
answer; it dispatches on `OP_LLM_PROVIDER` to `call_ollama()` (a model on
our own network) or `call_deepseek()` (the reviewed external option,
`docs/LLM_PROVIDERS.md`). Those two are the only functions that touch the
network, so they are the ones the tests monkeypatch to exercise the 422
(non-SELECT / non-JSON answer) paths without a real server, and nothing
else in the app knows or cares which service answered.
"""
from __future__ import annotations

import json
import re

import httpx

from app.config import settings

MAX_EXAMPLES_PER_COLUMN = 3
MAX_PARTITION_KEYS = 20

# Requests only ever come from this server process to a same-network Ollama
# endpoint the operator configured — never proxied, never following an
# operator-uncontrolled redirect.
_CONNECT_TIMEOUT_S = 5.0


class Nl2SqlError(Exception):
    """Raised for every failure the API route must turn into an HTTP error.
    `status_code` lets the route stay a thin translator instead of
    re-deriving what went wrong from the exception's type."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def build_prompt(tables: list[dict], values: dict[str, list[str]], question: str,
                 max_examples: int = 0) -> str:
    """Schema-only prompt. `tables` is the (already reach-scoped) catalog
    snapshot rows for exactly the `table_ids` the caller asked about;
    `values` is the snapshot's own `values` map (`"<table_id>.<column>"` ->
    up to 500 strings) — this function never queries anything itself, it
    only ever echoes back the first `max_examples` entries of whatever list
    it was handed, hard-capped at `MAX_EXAMPLES_PER_COLUMN` regardless of
    what the caller passes.

    `max_examples` defaults to 0 — the plan's "Security" contract
    ("the LLM gets only the schema … never values") — so callers must opt in
    (`settings.llm_example_values`) to put any real cell content in the
    prompt at all."""
    max_examples = max(0, min(max_examples, MAX_EXAMPLES_PER_COLUMN))
    lines = [
        "Ты переводишь вопрос пользователя на русском языке в один SQL-запрос "
        "для DuckDB (диалект PostgreSQL).",
        "Ниже — полная схема доступных таблиц. Используй ТОЛЬКО перечисленные "
        "таблицы и колонки — никогда не выдумывай их.",
        "Ответь ТОЛЬКО самим SQL-запросом (один SELECT или один WITH...SELECT), "
        "без пояснений, без markdown-разметки и без точки с запятой в конце.",
        "ВСЕ имена таблиц и колонок пиши в двойных кавычках ровно как в схеме — "
        "в них бывают пробелы и запятые: \"недопоставка, шт\", \"заказ, шт\".",
        "",
        "Схема:",
    ]
    for t in tables:
        table_name = t.get("name") or t.get("table_id")
        lines.append(f"\nТаблица \"{table_name}\" (table_id={t['table_id']}):")
        for c in t.get("columns", []):
            col_name = c.get("name")
            if not col_name:
                continue
            examples = (values.get(f"{t['table_id']}.{col_name}") or [])[:max_examples]
            ex_part = f", примеры значений: {', '.join(examples)}" if examples else ""
            # Quoted in the listing so the model copies the quoted spelling:
            # 1C headers carry spaces and commas ("short-delivery, pcs").
            lines.append(f"  - \"{col_name}\" (тип: {c.get('type') or 'неизвестен'}{ex_part})")
        keys = sorted({p.get("key") for p in t.get("partitions", []) if p.get("key")})
        if keys:
            shown = keys[:MAX_PARTITION_KEYS]
            more = "" if len(keys) <= MAX_PARTITION_KEYS else f" (и ещё {len(keys) - MAX_PARTITION_KEYS})"
            lines.append(f"  партиции: {', '.join(shown)}{more}")
    lines.append("")
    lines.append(f"Вопрос: {question}")
    lines.append("SQL:")
    return "\n".join(lines)


MAX_INTENT_KPIS = 40
MAX_INTENT_GLOSSARY = 24
INTENT_OPS = ("=", "!=", ">", ">=", "<", "<=", "in", "like")


_WORD_RE = re.compile(r"[a-zа-яё0-9]+")
MAX_PROMPT_KPIS = 6


def _stem(w: str) -> str:
    # Crude Russian stemming: enough to make an inflected "hub" or "chains" meet its base form.
    w = w.lower().replace("ё", "е")
    for suf in ("ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ах", "ях", "ам", "ям", "ов", "ев",
                "ой", "ей", "ом", "ем", "ою", "ею", "ую", "юю", "ая", "яя", "ые", "ие", "ий", "ый",
                "а", "я", "ы", "и", "у", "ю", "е", "о", "ь"):
        if len(w) > 4 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def rank_kpis(sem: dict, question: str, ready: list[str] | None) -> list[str]:
    """Keys of the KPIs most related to the question, by word overlap with
    their names, question forms, definitions and dims — a generic narrowing
    of the menu before the model sees it. Fewer candidates = shorter prompt
    on a CPU-only Ollama and less room to drift to a look-alike KPI. Keys with
    zero overlap are appended after the scored ones (up to the cap), so a
    question phrased unlike any form still gets a menu."""
    kpis = sem.get("kpis") or {}
    pool = [k for k in kpis if not ready or k in ready]
    q_words = {_stem(w) for w in _WORD_RE.findall(question.lower()) if len(w) > 2}
    scored = []
    for i, key in enumerate(pool):
        k = kpis[key]
        text = " ".join([str(k.get("name") or ""), str(k.get("short") or ""), str(k.get("definition") or ""),
                         " ".join(str(x) for x in (k.get("question_forms") or [])),
                         " ".join(str(x) for x in (k.get("dims") or []))])
        words = {_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) > 2}
        forms = " ".join(str(x).lower() for x in (k.get("question_forms") or []))
        score = len(q_words & words) + sum(2 for w in q_words if len(w) > 3 and w in forms)
        scored.append((-score, i, key))
    scored.sort()
    return [key for _, _, key in scored[:MAX_PROMPT_KPIS]]


# ─────────────────────────── form-first routing ────────────────────────────
# A weak CPU model is the slowest and least reliable part of "Ask in
# Russian". Most real questions are near-quotes of a `question_forms`
# entry the report's author already wrote down, so match them here, in
# Python, and never start Ollama at all. Nothing below knows anything about
# any particular report: the only inputs are the KPI's own `question_forms`,
# `name` and `short` from semantics.json (ADR-13 — report knowledge is data).

# Russian function words. Dropped from BOTH sides of the comparison, so the
# overlap counts content words only ("SL by chain" vs "SL by chain in week
# 27" must not score for "by"/"in"). Interrogatives ("which", "how many")
# are deliberately NOT here: authors write forms as whole questions
# ("which hub", "which hub has the highest SL"), so they carry signal.
_STOPWORDS = {
    "и", "в", "во", "на", "с", "со", "по", "за", "из", "к", "ко", "у", "о",
    "об", "от", "до", "для", "при", "над", "под", "про", "без", "через",
    "же", "ли", "бы", "не", "ни", "а", "но", "или", "то",
}
_NON_WORD_RE = re.compile(r"[^a-zа-я0-9]+")

# Confidence rule, in one place so the test and the docs quote the same
# numbers: a form match is CONFIDENT when the best KPI scores at least
# FORM_MIN_SCORE and at least FORM_LEAD × the runner-up KPI. A tie can
# therefore never be confident (2 × best > best), and one shared content
# word can never be confident on its own.
FORM_MIN_SCORE = 4
FORM_LEAD = 2


def _norm_text(s: str) -> str:
    """Lowercased, yo-folded (to ye), punctuation-to-space, space-padded — the shape
    both sides of the exact-form substring test are compared in."""
    return " " + _NON_WORD_RE.sub(" ", str(s).lower().replace("ё", "е")).strip() + " "


def _content_stems(s: str) -> set[str]:
    """Stemmed content words of `s` (function words and 1-letter tokens out).
    Keeps 2-letter tokens: «SL» is the single most load-bearing word in this
    report family and `rank_kpis`'s `len > 2` filter drops it."""
    out = set()
    for w in _WORD_RE.findall(str(s).lower().replace("ё", "е")):
        if len(w) < 2 or w in _STOPWORDS:
            continue
        st = _stem(w)
        if st not in _STOPWORDS:
            out.add(st)
    return out


def _form_score(q_stems: set[str], q_norm: str, form: str) -> int:
    """Score of ONE form against the question: shared content stems, plus
    2 points per stem of the form when the whole form appears verbatim
    inside the question (an author's phrase quoted back at us is the
    strongest signal there is — "which hub under-delivered most in July?"
    contains "which hub under-delivered most" word for word)."""
    f_stems = _content_stems(form)
    if not f_stems:
        return 0
    score = len(q_stems & f_stems)
    if len(f_stems) >= 2 and _norm_text(form).strip() and _norm_text(form) in q_norm:
        score += 2 * len(f_stems)
    return score


def kpi_forms(kpi: dict) -> list[str]:
    """Every phrase a KPI may be recognised by: its declared question forms
    first, then its short label and full name."""
    forms = [str(x) for x in (kpi.get("question_forms") or []) if str(x).strip()]
    for extra in (kpi.get("short"), kpi.get("name")):
        if extra and str(extra).strip():
            forms.append(str(extra))
    return forms


_RESIDUE_TOKEN_RE = re.compile(r"[A-Za-z\u0410-\u044f\u0401\u04510-9][\w\-\.]*")
_LATINISH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.]*$")
_QUOTED_RE = re.compile(r"[\u00ab\"\']([^\u00ab\u00bb\"\']{2,60})[\u00bb\"\']")


def residue_words(question: str, sem: dict, intent: dict) -> list[str]:
    """Value-shaped words of the question that NOTHING consumed — not the
    KPI's own vocabulary, not a dim name, not a glossary term, not a filter
    value already in the intent. The page hands them to the ENGINE, which
    probes them against actual dim values (`MBU` -> unit_name); names never
    reach any model from here. Conservative on purpose: latin/digit tokens
    and «quoted» phrases only — free Russian words are the question's own
    prose, and a false clarification is worse than none."""
    consumed: set = set()

    def eat(text):
        for w in _WORD_RE.findall(str(text or "").lower().replace("\u0451", "\u0435")):
            consumed.add(w)
            consumed.add(_stem(w))

    for k in (sem.get("kpis") or {}).values():
        if not isinstance(k, dict):
            continue
        eat(k.get("name")); eat(k.get("short")); eat(k.get("definition"))
        for f in (k.get("question_forms") or []):
            eat(f)
        for d in (k.get("dims") or []):
            eat(d)
    for tname, t in (sem.get("tables") or {}).items():
        eat(tname)
        for cname in ((t.get("columns") or {}) if isinstance(t, dict) else {}):
            eat(cname)
    for term in (sem.get("glossary") or {}):
        eat(term)
    for f in (intent.get("filters") or []):
        if isinstance(f, dict):
            eat(f.get("dim")); eat(f.get("value"))

    out: list[str] = []
    for m in _QUOTED_RE.finditer(question or ""):
        ph = m.group(1).strip()
        if ph and ph.lower().replace("\u0451", "\u0435") not in consumed and ph not in out:
            out.append(ph)
    for tok in _RESIDUE_TOKEN_RE.findall(question or ""):
        if not _LATINISH_RE.match(tok) or tok.isdigit() or len(tok) < 2:
            continue
        low = tok.lower()
        if low in consumed or _stem(low) in consumed:
            continue
        if tok not in out:
            out.append(tok)
    return out[:4]


def match_forms(sem: dict, question: str, ready: list[str] | None = None) -> dict | None:
    """`{"kpi", "score", "runner_up", "form"}` when the question matches ONE
    KPI's forms confidently, else None (the caller falls through to the
    model). Deterministic, offline, and generic: the phrases come from the
    bundle's own semantics.json, never from platform code."""
    kpis = sem.get("kpis") or {}
    q_stems = _content_stems(question)
    if not q_stems:
        return None
    q_norm = _norm_text(question)
    scored: list[tuple[int, str, str]] = []
    for key in kpis:
        if ready and key not in ready:
            continue
        best_form, best = "", 0
        for form in kpi_forms(kpis[key]):
            s = _form_score(q_stems, q_norm, form)
            if s > best:
                best, best_form = s, form
        scored.append((best, key, best_form))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    best, key, form = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0
    if best < FORM_MIN_SCORE or best < FORM_LEAD * runner:
        return None
    return {"kpi": key, "score": best, "runner_up": runner, "form": form}


FORM_LIMIT = 10


_TIME_DIMS = {"year", "month", "week", "day"}


def _dims_named(question: str, allowed: list[str]) -> list[str]:
    """Dims the question NAMES literally ("which sku…", "by material"):
    a dim whose own name appears as a word of the question is the cut the
    asker wants. Only exact dim-name words count — nothing is inferred, so
    this stays deterministic and generic; time dims are excluded because the
    period extraction owns them ("in week 32" is a filter, not a cut)."""
    words = set(_WORD_RE.findall(question.lower()))
    return [d for d in allowed
            if d.lower() not in _TIME_DIMS and d.lower() in words]


def form_intent(sem: dict, key: str, question: str) -> dict:
    """The intent a confident form match stands for, built WITHOUT the model:
    the KPI, the cut only when the question literally names a dim (else the
    metrics panel lets the operator pick it), the period the question
    actually names (same extraction `ground_intent` applies to the model's
    answer — nothing invented), biggest first, top-10."""
    kpi = (sem.get("kpis") or {}).get(key) or {}
    allowed = [str(d) for d in (kpi.get("dims") or [])]
    intent = ground_intent(question, {
        "kpi": key, "dims": _dims_named(question, allowed), "filters": [],
        "order": {"by": "value", "dir": "desc"}, "limit": FORM_LIMIT,
        "explanation": f"вопрос совпал с формулировкой метрики «{kpi.get('short') or kpi.get('name') or key}»",
        "_allowed_dims": allowed,
    })
    return intent


# ───────────────────────── "why" questions ─────────────────────────────────
# A "why did SL drop in January?" is not a KPI question with a superlative in
# it, but the intent form has no way to say so: the model picks a metric, the
# grounding pins the period, and the "the biggest" rule bolts an
# `ORDER BY value DESC LIMIT 1` onto a one-row aggregate. The result is SQL
# that parses, runs, and answers nothing.
#
# So "why" is classified BEFORE any prompt is built, and a why-question
# never reaches the normal intent prompt at all. The page then assembles a
# CARD (current period, previous period, where the number is concentrated)
# out of several deterministic intent queries — see `whyCard` in
# static/js/explore.js. Everything here is deterministic and generic: the
# only report knowledge involved is the KPI's own dims from semantics.json.

_WHY_PHRASES = ("из-за чего", "что случилось", "что произошло",
                "почему", "зачем", "отчего")
# Anchored at the start of the question — the shape a why-question actually
# takes in this report family ("why in January 2026 did SL drop?").
_WHY_LEAD_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(p) for p in _WHY_PHRASES) + r")\b", re.IGNORECASE)
# …and anywhere in the first three words, for "and why did SL drop?" /
# "SL: why?" — far enough in to still be the question's own verb, not an
# "unclear why" buried in a long sentence.
_WHY_HEAD_WORDS = 3
# Stripped from BOTH the KPI matching and the prompt: the model must never
# see the word, or it starts explaining instead of naming a metric.
_WHY_STRIP_RE = re.compile(
    r"(?<![a-zа-я])(" + "|".join(re.escape(p) for p in _WHY_PHRASES) + r")(?![a-zа-я])",
    re.IGNORECASE)


def classify_question(question: str) -> str:
    """`"why"` for a question asking for a REASON, `"kpi"` for everything
    else. Deterministic, offline, and the first thing every intent path
    does — the class decides which shape of answer is even possible."""
    q = str(question or "").replace("ё", "е")
    if _WHY_LEAD_RE.match(q):
        return "why"
    head = _WORD_RE.findall(q.lower())[:_WHY_HEAD_WORDS]
    if "почему" in head:
        return "why"
    return "kpi"


def strip_why(question: str) -> str:
    """The question with every why-word removed — what the KPI matcher and
    the prompt see. "why in January 2026 did SL drop?" -> "in January 2026 did SL
    drop?": the metric and the period survive, the interrogative does not."""
    out = _WHY_STRIP_RE.sub(" ", str(question or ""))
    out = re.sub(r"\s+", " ", out).strip(" \t,.;:?!-—")
    return out or str(question or "").strip()


def why_intent(sem: dict, key: str, question: str) -> dict:
    """The base intent of a why-card: the KPI, NO grouping, and only the
    period the question actually names. No `order` and no `limit` — the
    superlative rule is exactly what turned this question into nonsense SQL,
    and a "top-1 of one row" is not an explanation of anything."""
    kpi = (sem.get("kpis") or {}).get(key) or {}
    allowed = [str(d) for d in (kpi.get("dims") or [])]
    label = kpi.get("short") or kpi.get("name") or key
    return ground_intent(question, {
        "kpi": key, "dims": [], "filters": [],
        "explanation": f"вопрос о причинах: разбираю метрику «{label}»",
        "_allowed_dims": allowed,
    })


def why_intent_from_raw(raw: str, sem: dict, question: str) -> dict:
    """Model text -> a why-card intent. The model is asked one thing only —
    WHICH metric — and `parse_intent` validates that against the frozen
    semantics; everything else it wrote (dims, order, limit) is discarded
    here rather than trusted, so the shape of a why-answer cannot drift."""
    picked = parse_intent(raw, sem)
    return why_intent(sem, picked["kpi"], question)


# The dims a period is expressed in — the ADR-13 convention that time dims
# are named exactly `year`/`month`/`week`/`day`, finest first.
SHIFT_DIMS = ("day", "week", "month", "year")
_ISO_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def shift_period(filters: list | None) -> list | None:
    """`filters` moved one period back, or None when they name no period.

    The finest time dim present is the one that steps back, carrying into
    the coarser ones when it wraps: month 1 -> year-1/month 12, week 1 ->
    year-1/week 52, day -> the previous calendar day. Non-time filters are
    carried over untouched — "why did SL drop in January at 'Romashka'" compares
    the same retailer, one month earlier.

    Only `=` filters can be shifted: an `in` over three months has no single
    predecessor, so a period expressed that way is reported as unshiftable
    (None) rather than guessed at."""
    src = [dict(f) for f in (filters or []) if isinstance(f, dict)]
    idx: dict[str, int] = {}
    for i, f in enumerate(src):
        dim = str(f.get("dim") or "")
        if dim in SHIFT_DIMS and str(f.get("op") or "=") == "=":
            idx[dim] = i
    if not idx:
        return None

    def _num(dim: str) -> int | None:
        try:
            return int(str(src[idx[dim]].get("value")).strip())
        except (TypeError, ValueError):
            return None

    def _set(dim: str, val) -> None:
        src[idx[dim]]["value"] = val

    if "day" in idx:
        return _shift_day(src, idx)
    if "week" in idx:
        w = _num("week")
        if w is None:
            return None
        if w > 1:
            _set("week", w - 1)
            return src
        # ISO week 1 of a year: the previous period is the last week of the
        # year before. 52 is the honest default — a 53-week year would need
        # the calendar, and the query answers empty rather than wrong.
        _set("week", 52)
        return _step_year_back(src, idx, _num, _set)
    if "month" in idx:
        m = _num("month")
        if m is None:
            return None
        if m > 1:
            _set("month", m - 1)
            return src
        _set("month", 12)
        return _step_year_back(src, idx, _num, _set)
    y = _num("year")
    if y is None:
        return None
    _set("year", y - 1)
    return src


def _step_year_back(src: list, idx: dict, _num, _set) -> list | None:
    if "year" not in idx:
        return src              # no year in the question — nothing to carry
    y = _num("year")
    if y is None:
        return None
    _set("year", y - 1)
    return src


def _shift_day(src: list, idx: dict) -> list | None:
    """The previous calendar day. `day` is either a full ISO date (the shape
    a date dim usually carries) or a day-of-month number, which only has a
    predecessor when the question also pinned the year and the month."""
    import datetime as _dt
    raw = str(src[idx["day"]].get("value") or "").strip()
    m = _ISO_DAY_RE.match(raw)
    if m:
        try:
            d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - _dt.timedelta(days=1)
        except ValueError:
            return None
        src[idx["day"]]["value"] = d.isoformat()
        if "month" in idx:
            src[idx["month"]]["value"] = d.month
        if "year" in idx:
            src[idx["year"]]["value"] = d.year
        return src
    try:
        day = int(raw)
    except ValueError:
        return None
    try:
        year = int(str(src[idx["year"]]["value"]).strip()) if "year" in idx else None
        month = int(str(src[idx["month"]]["value"]).strip()) if "month" in idx else None
    except (TypeError, ValueError):
        year = month = None
    if year and month:
        try:
            d = _dt.date(year, month, day) - _dt.timedelta(days=1)
        except ValueError:
            return None
        src[idx["day"]]["value"] = d.day
        src[idx["month"]]["value"] = d.month
        src[idx["year"]]["value"] = d.year
        return src
    if day > 1:
        src[idx["day"]]["value"] = day - 1
        return src
    # Day 1 with no year/month to fall back into: the previous month's length
    # is unknowable from the filters alone.
    return None


def build_intent_schema(sem: dict, shortlist: list[str] | None,
                        fixed_kpi: str | None = None) -> dict:
    """JSON schema for Ollama's constrained decoding (`format`, Ollama ≥0.5):
    the model can only emit a KPI key from the shortlist, a dim name one of
    those KPIs actually has, and an op we implement — so the classes of
    answer `parse_intent` used to throw away (invented KPI, invented dim,
    prose instead of JSON) become unrepresentable rather than rejected.
    `parse_intent` stays the validator: a schema is a decoding constraint,
    not a trust boundary, and Ollama may ignore it.

    `fixed_kpi` is the one-shot form path (the metric is already chosen by
    the form matcher and the model is only asked for the CUT): the kpi enum
    collapses to that single key with no `null` option — declining is not an
    available answer when the question already matched a metric."""
    kpis = sem.get("kpis") or {}
    keys = [k for k in (shortlist if shortlist is not None else list(kpis)) if k in kpis]
    dims = sorted({str(d) for k in keys for d in (kpis[k].get("dims") or [])})
    dim_schema = {"type": "string", "enum": dims} if dims else {"type": "string"}
    order_by = ["value", "n", "d"] + dims
    if fixed_kpi and fixed_kpi in kpis:
        kpi_schema = {"type": "string", "enum": [fixed_kpi]}
    else:
        # null is a first-class answer: "this question is not about any
        # of these metrics" is what makes the 422 fall-through honest.
        kpi_schema = {"type": ["string", "null"], "enum": keys + [None]}
    return {
        "type": "object",
        "properties": {
            "kpi": kpi_schema,
            "dims": {"type": "array", "items": dim_schema},
            "filters": {"type": "array", "items": {
                "type": "object",
                "properties": {"dim": dim_schema,
                               "op": {"type": "string", "enum": list(INTENT_OPS)},
                               "value": {}},
                "required": ["dim", "op", "value"],
            }},
            "order": {"type": "object",
                      "properties": {"by": {"type": "string", "enum": order_by},
                                     "dir": {"type": "string", "enum": ["asc", "desc"]}},
                      "required": ["by", "dir"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "explanation": {"type": "string"},
        },
        "required": ["kpi", "dims", "filters", "limit", "explanation"],
    }


def build_intent_prompt(sem: dict, ready_kpis: list[str] | None, question: str,
                        extra_glossary: dict | None = None,
                        fixed_kpi: str | None = None,
                        prev_line: str | None = None) -> str:
    """Intent prompt (ADR-13): the model does not write SQL, it fills a form.
    Only definitions go in — KPI keys, names, question forms, dims, the
    glossary — never a cell value and never the KPI SQL (the engine owns
    that). Kept small on purpose: a CPU-only Ollama pays per prompt token.

    `extra_glossary` (H2, platform glossary): terms->definitions the caller
    already reach-scoped (company-wide + the generator's own), listed BEFORE
    the semantics glossary so a platform definition wins visually when a term
    appears in both; the combined list still respects `MAX_INTENT_GLOSSARY`.

    `fixed_kpi` (one-shot form path): the metric was already chosen — by the
    form matcher, deterministically — and the ONLY thing left for the model
    is the cut: dims, filters, order, limit. Choosing a metric is then not on
    the table at all, which is both a shorter prompt and one less way for a
    weak model to drift off the KPI the question literally quoted."""
    kpis = sem.get("kpis") or {}
    if fixed_kpi and fixed_kpi in kpis:
        return _build_fixed_kpi_prompt(sem, fixed_kpi, question, extra_glossary, prev_line)
    keys = [k for k in kpis if not ready_kpis or k in ready_kpis][:MAX_INTENT_KPIS]
    lines = [
        "Ты переводишь вопрос пользователя на русском языке в выбор метрики отчёта.",
        "Ответь ТОЛЬКО одним JSON-объектом без пояснений и без markdown:",
        '{"kpi": "<ключ метрики>", "dims": ["<измерение>", ...], '
        '"filters": [{"dim": "<измерение>", "op": "=|!=|>|>=|<|<=|in|like", "value": <значение или список>}], '
        '"order": {"by": "value|<измерение>", "dir": "asc|desc"}, "limit": <число>, '
        '"explanation": "<одна фраза по-русски, почему выбрана эта метрика>"}',
        "Используй ТОЛЬКО перечисленные ключи метрик и их измерения. Если вопрос не про "
        "одну из метрик — верни {\"kpi\": null, \"explanation\": \"...\"}.",
        "Правила: выбирай самую узкую метрику, которая отвечает на вопрос; в dims клади "
        "ТОЛЬКО измерения, по которым спрашивают (не все подряд); период — это filters по "
        "year/month/week/day, а не dims; «самый большой / у какого» = order по value desc и "
        "limit 1 (или 5, если просят список); «первая неделя июля» = filters month=7 и "
        "week = первая ISO-неделя месяца. Если просят ПОКАЗАТЬ СТРОКИ/СПИСОК записей "
        "(«покажи все строки…», «выведи заказы…»), а не значение метрики — верни "
        "{\"kpi\": null, \"explanation\": \"это выборка строк, а не метрика — используйте конструктор запросов\"}.",
        "",
        "Метрики:",
    ]
    lines += _kpi_menu_lines(sem, keys)
    lines += _dim_lines(sem, keys)
    lines += _glossary_lines(sem, extra_glossary)
    lines.append("")
    if prev_line:
        lines.append(prev_line)
    lines.append(f"Вопрос: {question}")
    lines.append("JSON:")
    return "\n".join(lines)


def _kpi_menu_lines(sem: dict, keys: list[str]) -> list[str]:
    """The metrics menu itself (the prompt's "Metrics:" block) — one line per KPI, definitions only.
    Shared by the intent prompt and the why prompt so a KPI is described the
    same way whichever question type is being answered."""
    kpis = sem.get("kpis") or {}
    out = []
    for key in keys:
        k = kpis.get(key) or {}
        forms = ", ".join(str(x) for x in (k.get("question_forms") or [])[:6])
        dims = ", ".join(str(d) for d in (k.get("dims") or []))
        out.append(f"- {key}: {k.get('name') or ''}. {k.get('definition') or ''}"
                   + (f" Как спрашивают: {forms}." if forms else "")
                   + (f" Измерения: {dims}." if dims else ""))
    return out


MAX_DIM_LINES = 40
_DIM_DESC_CHARS = 90


def _dim_lines(sem: dict, keys: list[str]) -> list[str]:
    """The dimensions block of the prompt: every dim of the listed KPIs, once, with the
    author's own description of that column (semantics `tables.*.columns` /
    `views.*.derived_columns`). A dim is an identifier the model has never
    seen (`rdc`, `shipping_display`); the description is the word the user
    actually says ("branch", "warehouse"). Dims with no description are listed
    bare — still names only, never a value."""
    kpis = sem.get("kpis") or {}
    seen: list[str] = []
    for key in keys:
        for d in ((kpis.get(key) or {}).get("dims") or []):
            d = str(d)
            if d not in seen:
                seen.append(d)
    if not seen:
        return []
    from app.services import semantics as _sem
    desc = _sem.column_descriptions(sem)
    out = ["", "Измерения (что значит каждое):"]
    for d in seen[:MAX_DIM_LINES]:
        text = desc.get(d, "")
        out.append(f"- {d}" + (f": {text[:_DIM_DESC_CHARS]}" if text else ""))
    return out


def build_why_prompt(sem: dict, shortlist: list[str] | None, question: str,
                     extra_glossary: dict | None = None) -> str:
    """The why path's prompt: name the METRIC, nothing else.

    The card's shape (period, previous period, concentration) is decided by
    the engine — `why_intent_from_raw` keeps only the key — so asking for
    dims, order and a limit would be asking for answers that get thrown
    away, and the superlative instruction in the normal intent prompt is the
    very thing that produced «ORDER BY value DESC LIMIT 1» over a one-row
    aggregate. The JSON shape still matches `build_intent_schema`, so
    constrained decoding works unchanged.

    The question reaching this function is already `strip_why`-ed, and no
    line of this prompt uses a why-word either: a model told it is explaining
    something starts writing prose instead of picking a metric."""
    kpis = sem.get("kpis") or {}
    keys = [k for k in (shortlist if shortlist is not None else list(kpis)) if k in kpis][:MAX_INTENT_KPIS]
    lines = [
        "Ты выбираешь, о какой метрике отчёта идёт речь в вопросе пользователя.",
        "Ответь ТОЛЬКО одним JSON-объектом без пояснений и без markdown:",
        '{"kpi": "<ключ метрики>", "dims": [], "filters": [], "limit": 1, '
        '"explanation": "<одна фраза по-русски о выборе метрики>"}',
        "Нужен ТОЛЬКО ключ метрики из списка ниже: разрез, период и сортировку "
        "движок задаёт сам, поэтому dims и filters всегда пустые. Если вопрос не "
        "про одну из метрик — верни {\"kpi\": null, \"explanation\": \"...\"}.",
        "",
        "Метрики:",
    ]
    lines += _kpi_menu_lines(sem, keys)
    lines += _glossary_lines(sem, extra_glossary)
    lines.append("")
    lines.append(f"Вопрос: {question}")
    lines.append("JSON:")
    return "\n".join(lines)


def _glossary_lines(sem: dict, extra_glossary: dict | None) -> list[str]:
    """The terms (glossary) block shared by every prompt this module builds: the
    platform glossary first (it wins visually when a term is defined twice),
    then the bundle's own, capped at `MAX_INTENT_GLOSSARY`."""
    glossary = dict(extra_glossary or {})
    for term, desc in (sem.get("glossary") or {}).items():
        glossary.setdefault(term, desc)
    if not glossary:
        return []
    out = ["", "Термины:"]
    for term, desc in list(glossary.items())[:MAX_INTENT_GLOSSARY]:
        out.append(f"- {term}: {str(desc)[:160]}")
    return out


def _build_fixed_kpi_prompt(sem: dict, key: str, question: str,
                            extra_glossary: dict | None = None,
                            prev_line: str | None = None) -> str:
    """The one-shot form path's prompt: the metric is already chosen, fill in
    the cut. Everything the model could get wrong about WHICH number to
    compute has already been decided deterministically by `match_forms`, so
    this prompt never mentions another KPI."""
    k = (sem.get("kpis") or {})[key]
    dims = [str(d) for d in (k.get("dims") or [])]
    label = k.get("short") or k.get("name") or key
    lines = [
        f"Метрика уже выбрана: «{label}» (ключ {key}). Менять её нельзя.",
        "Твоя задача — только разрез и период: по каким измерениям сгруппировать, "
        "какие фильтры наложить, как отсортировать и сколько строк показать.",
        "Ответь ТОЛЬКО одним JSON-объектом без пояснений и без markdown:",
        f'{{"kpi": "{key}", "dims": ["<измерение>", ...], '
        '"filters": [{"dim": "<измерение>", "op": "=|!=|>|>=|<|<=|in|like", "value": <значение или список>}], '
        '"order": {"by": "value|<измерение>", "dir": "asc|desc"}, "limit": <число>, '
        '"explanation": "<одна фраза по-русски, какой разрез выбран>"}',
        "Правила: в dims клади ТОЛЬКО измерения, по которым спрашивают (не все подряд); "
        "если разрез не назван — верни пустой dims (это итог по всей метрике); период — "
        "это filters по year/month/week/day, а не dims; «самый большой / у какого» = "
        "order по value desc и limit 1 (или 5, если просят список).",
        "",
        f"Метрика {key}: {k.get('name') or ''}. {k.get('definition') or ''}",
    ]
    if dims:
        lines.append("Доступные измерения (других не существует): " + ", ".join(dims))
        lines += _dim_lines(sem, [key])
    lines += _glossary_lines(sem, extra_glossary)
    lines.append("")
    if prev_line:
        lines.append(prev_line)
    lines.append(f"Вопрос: {question}")
    lines.append("JSON:")
    return "\n".join(lines)


# ────────────────────── tier 2: free SQL over the report ───────────────────
# Tier 1 is the intent form: the model picks a KPI the report's author
# defined, and the engine writes the SQL. It answers only what somebody
# already thought to define. Tier 2 is the fall-through for everything else
# a question can be about ("how many rows have no reason?", "which chains grew
# versus last week?"): the model gets the report's own SEMANTIC schema — the
# canonical tables and views, their columns, their types and the author's
# Russian descriptions — and writes one SELECT over it.
#
# What that costs and why it is still safe:
#   * the prompt is names and prose, never a cell (pinned by a test);
#   * the SQL is MODEL-WRITTEN, so it is labelled as such in the UI;
#   * `/semantic-sql/validate` runs `is_select_only` AND the worker's own
#     `guard_sql` before the page ever sees it, and the browser worker runs
#     that same guard again before DuckDB — which is read-only anyway.
MAX_SEMANTIC_RELATIONS = 24
MAX_SEMANTIC_RULES = 10
_RULE_CHARS = 300


def _relation_lines(kind: str, name: str, spec: dict) -> list[str]:
    head = f"\n{kind} \"{name}\""
    desc = str(spec.get("description") or "").strip()
    if desc:
        head += f" — {desc}"
    grain = str(spec.get("grain") or "").strip()
    if grain:
        head += f" (гранулярность: {grain})"
    out = [head + ":"]
    for c in spec.get("columns") or []:
        cdesc = str(c.get("description") or "").strip()
        out.append(f"  - \"{c['name']}\" ({c.get('type') or 'text'})"
                   + (f" — {cdesc}" if cdesc else ""))
    return out


def _rules_lines(sem: dict) -> list[str]:
    """The author's prose rules — what the generator already did to the data
    before any of these columns existed (which files it read, how it parsed
    dates, which rows it dropped). Without them a model writes SQL that
    double-applies a filter the view already applied."""
    rules = sem.get("rules") or {}
    if not isinstance(rules, dict) or not rules:
        return []
    out = ["", "Правила отчёта (уже применены к данным — не повторяй их в SQL):"]
    for name, val in list(rules.items())[:MAX_SEMANTIC_RULES]:
        text = val.get("rule") if isinstance(val, dict) else val
        if not isinstance(text, str) or not text.strip():
            continue
        out.append(f"- {name}: {text.strip()[:_RULE_CHARS]}")
    return out if len(out) > 2 else []


def build_semantic_sql_prompt(sem: dict, bindings_status: dict | None, question: str,
                              extra_glossary: dict | None = None,
                              outline: dict | None = None) -> str:
    """Tier 2's prompt: this report's semantic schema + one question -> one
    SELECT. `bindings_status` is the worker's own `semantics_status()` entry
    for this generator (`{"tables": {name: "bound"|"inline"|"unbound"},
    "views": {name: "ok"|"materialized"|"error"}}`) — only relations that actually stand on
    the folder the user indexed are described, because a table the engine
    could not build is a table the SQL must not name. `None` means "describe
    everything declared" (the server-side path, before any folder is known).

    Pure: no I/O beyond `semantics.schema_outline`'s empty-table dry run, and
    no path from here to a cell value — the outline carries names, types and
    the author's descriptions only. Pass `outline` to reuse one a caller
    already computed (that dry run is the only expensive part of this path).
    """
    if outline is None:
        outline = _sem_outline(sem)
    t_status = (bindings_status or {}).get("tables") or {}
    v_status = (bindings_status or {}).get("views") or {}

    def _ok(status: dict, name: str, good: tuple) -> bool:
        return True if bindings_status is None else status.get(name) in good

    lines = [
        "Ты переводишь вопрос пользователя на русском языке в один SQL-запрос "
        "для DuckDB (диалект PostgreSQL) по семантической модели отчёта.",
        "Ниже — все доступные таблицы и представления этого отчёта с описанием "
        "каждой колонки. Используй ТОЛЬКО их — никогда не выдумывай имена.",
        "Ответь ТОЛЬКО самим SQL-запросом (один SELECT или один WITH...SELECT), "
        "без пояснений, без markdown-разметки и без точки с запятой в конце.",
        "ВСЕ имена таблиц и колонок пиши в двойных кавычках ровно как в схеме.",
        "Всегда ставь LIMIT (не больше 1000). Ничего, кроме SELECT, писать нельзя.",
        "",
        "Схема отчёта:",
    ]
    shown = 0
    for name, spec in outline.get("views", {}).items():
        if shown >= MAX_SEMANTIC_RELATIONS or not _ok(v_status, name, ("ok", "materialized")):
            continue
        lines += _relation_lines("Представление", name, spec)
        shown += 1
    for name, spec in outline.get("tables", {}).items():
        if shown >= MAX_SEMANTIC_RELATIONS or not _ok(t_status, name, ("bound", "inline", "reference")):
            continue
        lines += _relation_lines("Таблица", name, spec)
        shown += 1
    lines += _glossary_lines(sem, extra_glossary)
    lines += _rules_lines(sem)
    lines.append("")
    lines.append(f"Вопрос: {question}")
    lines.append("SQL:")
    return "\n".join(lines)


def semantic_relations(sem: dict, bindings_status: dict | None,
                       outline: dict | None = None) -> list[str]:
    """The relation names tier 2 offered the model, for the audit line and
    for the page's "over which views" status."""
    if outline is None:
        outline = _sem_outline(sem)
    t_status = (bindings_status or {}).get("tables") or {}
    v_status = (bindings_status or {}).get("views") or {}
    out = [n for n in outline.get("views", {})
           if bindings_status is None or v_status.get(n) in ("ok", "materialized")]
    out += [n for n in outline.get("tables", {})
            if bindings_status is None or t_status.get(n) in ("bound", "inline", "reference")]
    return out[:MAX_SEMANTIC_RELATIONS]


def _sem_outline(sem: dict) -> dict:
    from app.services import semantics as _sem
    return _sem.schema_outline(sem)


_RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
    "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_TIME_DIMS = {"year", "month", "week", "day", "date"}


def _question_times(question: str) -> dict:
    """Time values the question actually names: a 4-digit year, a month by
    Russian name or `month=N`-style digit next to the word "month", a week number next
    to the stem of "week". A small model happily invents `year = 2023`; nothing it did
    not read may survive into a filter."""
    q = question.lower()
    out: dict[str, set] = {"year": set(), "month": set(), "week": set()}
    for y in re.findall(r"\b(20\d\d)\b", q):
        out["year"].add(int(y))
    for stem, num in _RU_MONTHS.items():
        if stem == "ма":
            if re.search(r"\bма[яе]\b", q):
                out["month"].add(5)
            continue
        if re.search(r"\b" + stem + r"[а-я]*", q):
            out["month"].add(num)
    # "week 2026" is a YEAR after the word, not week 20: the number after
    # the "week" stem must end where it starts (stand 2026-09-06: week {20, 23}).
    for w in re.findall(r"(?<!\d)(\d{1,2})\s*-?\s*(?:й|я|ой|ую|ая|ый)?\s*недел", q) + re.findall(r"недел[а-я]*\s*(?:№\s*)?(\d{1,2})(?!\d)", q):
        out["week"].add(int(w))
    if re.search(r"перв[а-я]*\s+недел", q) and out["month"]:
        # first ISO week of the named month (2026): Jan 1 is a Thursday
        import datetime as _dt
        for m in list(out["month"]):
            y = next(iter(out["year"]), 2026)
            out["week"].add(_dt.date(y, m, 1).isocalendar()[1])
    return out


def has_metric_words(sem: dict, question: str, ready: list[str] | None = None) -> bool:
    """Does the question name a METRIC at all? True when a content stem of the
    question is also a content stem of some KPI's own forms (`question_forms`,
    short label, name) — the same vocabulary `match_forms` scores against, so
    the two can never disagree about what "a metric word" is.

    This is the guard on the follow-up path: "and by week?" carries no metric
    word, so the previous metric may be pinned; "what is SL by week?" does,
    so the model must be free to pick a different one."""
    kpis = sem.get("kpis") or {}
    vocab: set[str] = set()
    for key in kpis:
        if ready and key not in ready:
            continue
        for form in kpi_forms(kpis[key]):
            vocab |= _content_stems(form)
    if not vocab:
        return False
    return bool(_content_stems(question) & vocab)


def prev_intent_line(sem: dict, prev: dict | None) -> str:
    """ONE prompt line describing the previous answer — NAMES ONLY.

    The metric's label, the dims it was cut by, and which TIME dims carried a
    period. Never a value: a filter's `value` is data read off the user's
    folder, and the data-boundary rule (docs/LLM_PROVIDERS.md) says definitions
    may reach a model and cell values may not. `ground_intent` is what actually
    carries the previous period's values forward, deterministically, on our
    side of that boundary."""
    if not prev or not prev.get("kpi"):
        return ""
    k = (sem.get("kpis") or {}).get(prev["kpi"]) or {}
    label = k.get("short") or k.get("name") or prev["kpi"]
    parts = [f"Предыдущий вопрос был про метрику «{label}» (ключ {prev['kpi']})"]
    dims = [str(d) for d in (prev.get("dims") or [])]
    parts.append("разрез: " + (", ".join(dims) if dims else "итог по всей метрике"))
    period = [str(f.get("dim")) for f in (prev.get("filters") or [])
              if isinstance(f, dict) and str(f.get("dim")) in _TIME_DIMS]
    if period:
        parts.append("период задан по: " + ", ".join(sorted(set(period))))
    return "; ".join(parts) + ". Это уточняющий вопрос к нему."


def ground_intent(question: str, intent: dict, prev_intent: dict | None = None) -> dict:
    """Deterministic clean-up after the model: drop time filters whose values
    the question never mentioned (hallucinated years/weeks), turn a named
    month into a filter when the model forgot it, and take a dim out of the
    grouping once it is pinned to one value by an equality filter.

    `prev_intent` (phase 3 follow-ups): "and by week?" names no period, and
    the honest reading is "the same period as the answer I am looking at" —
    not "all of history". So when the question names NO time value at all, the
    previous intent's time filters are inherited verbatim. A question that
    DOES name a period overrides them completely: the rule is inherit-or-be-
    told, never merge, which is the only version an operator can predict."""
    times = _question_times(question)
    filters = []
    for f in intent.get("filters") or []:
        dim = f["dim"]
        if dim in ("year", "month", "week") and f["op"] in ("=", "in"):
            vals = f.get("values") if f["op"] == "in" else [f.get("value")]
            try:
                nums = {int(str(v).strip()) for v in vals}
            except (TypeError, ValueError):
                continue
            allowed = times[dim]
            if not allowed:
                continue               # the question named no such thing
            keep = sorted(nums & allowed) or sorted(allowed)
            if len(keep) == 1:
                filters.append({"dim": dim, "op": "=", "value": keep[0]})
            else:
                filters.append({"dim": dim, "op": "in", "values": keep})
            continue
        filters.append(f)
    present = {f["dim"] for f in filters}
    allowed_dims = set(intent.get("_allowed_dims") or [])
    for dim in ("year", "month", "week"):
        # A period the question names is kept EVEN when this KPI has no such
        # cut: the engine drops it with a note the user sees ("the metric
        # has no breakdown by week"), whereas dropping it here made the verifier
        # ask "which week?" about a week the question had named (stand
        # 2026-09-06). `allowed_dims` still gates INHERITED periods below.
        if dim not in present and len(times[dim]) == 1:
            filters.append({"dim": dim, "op": "=", "value": next(iter(times[dim]))})
    # Inheritance: only when the question named no period whatsoever, and only
    # for dims this KPI actually has. `allowed_dims` is empty on the paths that
    # do not carry it (a hand-built intent), and inheriting into a dim the KPI
    # does not have would build SQL that cannot run.
    if prev_intent and not any(times[d] for d in ("year", "month", "week")):
        present = {f["dim"] for f in filters}
        # A dim the new question GROUPS BY is the question ("in which
        # week…" after a week-34 answer): inheriting week=34 would pin it
        # and delete the very cut asked for (prod 2026-08-28).
        grouped = {str(d) for d in (intent.get("dims") or [])}
        for f in prev_intent.get("filters") or []:
            if not isinstance(f, dict):
                continue
            dim = str(f.get("dim") or "")
            if dim not in _TIME_DIMS or dim in present or dim in grouped:
                continue
            if allowed_dims and dim not in allowed_dims:
                continue
            filters.append(dict(f))

    pinned = {f["dim"] for f in filters if f.get("op") == "="}
    dims = [d for d in (intent.get("dims") or []) if d not in pinned]
    out = dict(intent)
    out["filters"] = filters
    out["dims"] = dims
    out.pop("_allowed_dims", None)
    return out


def parse_intent(raw: str, sem: dict, fixed_kpi: str | None = None) -> dict:
    """Model text -> validated intent dict, or Nl2SqlError(422). Every name
    is checked against the semantics; values are passed through as data —
    the engine quotes them, never interpolates.

    `fixed_kpi` (one-shot form path) pins the metric: whatever the model
    wrote in `kpi` is ignored and the deterministically matched key stands.
    The model can then only ever contribute the cut, and a model that
    answered `{"kpi": null}` to a question the form matcher was confident
    about cannot turn that confidence into a 422."""
    text = strip_fences(raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise Nl2SqlError(422, "Модель не вернула JSON с выбором метрики; переформулируйте вопрос")
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        raise Nl2SqlError(422, "Модель вернула некорректный JSON; переформулируйте вопрос")
    if not isinstance(obj, dict):
        raise Nl2SqlError(422, "Модель вернула не объект; переформулируйте вопрос")
    kpis = sem.get("kpis") or {}
    key = fixed_kpi if (fixed_kpi and fixed_kpi in kpis) else obj.get("kpi")
    if not key:
        raise Nl2SqlError(422, str(obj.get("explanation") or "Вопрос не соответствует ни одной метрике отчёта"))
    if key not in kpis:
        raise Nl2SqlError(422, f"Модель выбрала неизвестную метрику {str(key)[:40]!r}")
    allowed = [str(d) for d in (kpis[key].get("dims") or [])]
    dims = [str(d) for d in (obj.get("dims") or []) if isinstance(d, (str, int))][:8]
    dims = [d for d in dims if d in allowed]
    filters = []
    for f in (obj.get("filters") or [])[:12]:
        if not isinstance(f, dict):
            continue
        dim, op = str(f.get("dim") or ""), str(f.get("op") or "=").lower()
        if dim not in allowed or op not in INTENT_OPS:
            continue
        val = f.get("value") if f.get("value") is not None else f.get("values")
        if val is None:
            continue
        if op == "in":
            vals = val if isinstance(val, list) else [val]
            filters.append({"dim": dim, "op": "in", "values": [str(v)[:200] for v in vals[:50]]})
        elif isinstance(val, (str, int, float, bool)):
            filters.append({"dim": dim, "op": op, "value": val if not isinstance(val, str) else val[:200]})
    order = obj.get("order") if isinstance(obj.get("order"), dict) else None
    measures = ["value", "n", "d"] if kpis[key].get("measure") == "ratio" else ["value"]
    if order and str(order.get("by")) not in dims + measures:
        order = None
    try:
        limit = max(1, min(int(obj.get("limit") or 100), 1000))
    except (TypeError, ValueError):
        limit = 100
    return {"kpi": key, "dims": dims, "filters": filters,
            "order": {"by": str(order["by"]), "dir": "desc" if str(order.get("dir", "")).lower() == "desc" else "asc"} if order else None,
            "limit": limit, "explanation": str(obj.get("explanation") or "")[:300],
            "_allowed_dims": allowed}


_FENCE_RE = re.compile(r"^```(?:sql|SQL)?\s*|\s*```$", re.MULTILINE)
_SQL_COMMENT_RE = re.compile(r"--.*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)
_SELECT_RE = re.compile(r"^(select|with)\b", re.IGNORECASE)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

# Same DML/DDL/engine-control words the worker's own `guard_sql`
# (static/py/_explore.py `_FORBIDDEN`) refuses — kept as a short, explicit
# subset here rather than importing the browser module: this is defence in
# depth server-side (the worker's guard is the real gate before DuckDB ever
# sees anything), so it only needs to catch a CTE- or comment-smuggled
# DML/DDL statement or an explicit file/pragma read, not every introspection
# function the worker also blocks.
_FORBIDDEN_WORDS = (
    "attach", "detach", "insert", "update", "delete", "drop", "create",
    "alter", "truncate", "merge", "upsert",
    "pragma", "set", "reset", "call", "install", "load", "copy", "export",
    "import", "checkpoint", "vacuum", "analyze", "begin", "commit",
    "rollback", "transaction", "prepare", "execute", "deallocate",
    "grant", "revoke", "use", "force",
    "read_csv", "read_csv_auto", "read_parquet", "read_json",
    "read_json_auto", "read_json_objects", "read_text", "read_blob",
    "parquet_scan", "csv_scan", "sniff_csv", "glob", "getenv",
    "load_extension", "install_extension", "duckdb_extensions",
)
_FORBIDDEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(sorted(_FORBIDDEN_WORDS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])", re.IGNORECASE)


def strip_fences(text: str) -> str:
    """Ollama models routinely wrap SQL in a ```sql fence even when told not
    to. Strip a leading/trailing fence (with or without a language tag) and
    surrounding whitespace; leave everything else untouched."""
    return _FENCE_RE.sub("", text).strip()


def is_select_only(sql: str) -> bool:
    """True iff `sql`, once comments are stripped, is exactly one SELECT or
    WITH...SELECT statement — no trailing statement, no DDL/DML smuggled in
    behind a semicolon, and no DDL/DML/engine-control keyword smuggled in
    behind a leading `WITH` (a CTE prefix satisfies the `^(select|with)`
    anchor even when the statement it introduces is `INSERT`/`ATTACH`/etc.).
    This is defence in depth, not the only gate — the browser worker's own
    `guard_sql` (static/py/_explore.py) is what actually stands between this
    SQL and DuckDB — but it is deliberately conservative anyway: a stray
    semicolon anywhere but the very end, or a forbidden word anywhere
    outside a string literal, fails closed."""
    stripped = _SQL_COMMENT_RE.sub(" ", sql).strip()
    stripped = stripped.rstrip(";").strip()
    if not stripped or ";" in stripped:
        return False
    if not _SELECT_RE.match(stripped):
        return False
    masked = _STRING_LITERAL_RE.sub(lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", stripped)
    return not _FORBIDDEN_RE.search(masked)


# Flipped to False the first time an endpoint answers 4xx to a `format`
# body — an Ollama older than 0.5 has no constrained decoding, and paying a
# rejected round trip per question (on a CPU box: seconds) forever is worse
# than losing the constraint. Single-writer discipline: only `call_ollama`
# writes it, and only ever True -> False.
_FORMAT_SUPPORTED = True


def call_ollama(prompt: str, fmt: dict | None = None) -> str:
    """POST the prompt to `OP_LLM_ENDPOINT`'s `/api/generate`, return the raw
    response text (fences still attached — the caller strips them). Raises
    `Nl2SqlError(503, ...)` for every network/config failure so the route
    never has to distinguish "no endpoint" from "endpoint unreachable" from
    "endpoint timed out" — all three mean "NL->SQL is unavailable right now".

    `fmt` is a JSON schema passed to Ollama as `format` (≥0.5: the sampler is
    constrained to tokens that keep the answer valid against it). An Ollama
    that does not understand the field answers 4xx; we log that ONCE, retry
    the same prompt without it, and stop sending `format` for the rest of the
    process lifetime.
    """
    endpoint = (settings.llm_endpoint or "").strip()
    if not endpoint:
        raise Nl2SqlError(503, "NL→SQL отключён: не настроен OP_LLM_ENDPOINT")
    url = endpoint.rstrip("/") + "/api/generate"
    if settings.llm_log:
        import logging
        logging.getLogger("onepage.nl2sql").info("prompt: %s", prompt)
    body = {"model": settings.llm_model, "prompt": prompt,
            # keep_alive holds the model in RAM between questions: on a
            # CPU-only Ollama a cold call pays the full model load (tens of
            # seconds) before the first prompt token, and the default
            # keep-alive evicts after 5 idle minutes.
            "stream": False, "keep_alive": "60m"}
    if fmt and _FORMAT_SUPPORTED:
        body = dict(body, format=fmt)
    timeout = httpx.Timeout(settings.llm_timeout_s, connect=_CONNECT_TIMEOUT_S)
    try:
        resp = httpx.post(url, json=body, timeout=timeout)
        if "format" in body and 400 <= resp.status_code < 500:
            _drop_format_support(resp.status_code)
            body.pop("format")
            resp = httpx.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise Nl2SqlError(503, f"LLM-сервис недоступен: {e}") from e
    except (ValueError, json.JSONDecodeError) as e:
        raise Nl2SqlError(503, f"LLM-сервис вернул не-JSON ответ: {e}") from e
    text = data.get("response") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise Nl2SqlError(503, "LLM-сервис вернул пустой ответ")
    return text


# ─────────────────────────── provider dispatch ────────────────────────────
# Two providers, one entry point. `call_llm` is what the routes and
# tools/intent_eval.py call; `call_ollama` stays the ollama implementation
# (and the function the old tests monkeypatch). Adding a provider means
# adding a branch here and a `_call_*` below — nothing else in the app knows
# which service answered.

DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# One short instruction in the system role; the schema and the definitions
# ride in the user message (built by build_intent_prompt/build_prompt), so
# the two providers see the SAME prompt text and stay comparable.
_DEEPSEEK_SYSTEM = ("Ты отвечаешь строго одним JSON-объектом или одним SQL-запросом — "
                    "тем, что просит пользователь. Без пояснений и без markdown.")
# DeepSeek's JSON mode is a MODE, not schema enforcement: it guarantees
# syntactically valid JSON and nothing about the keys. The schema therefore
# goes into the prompt as text, and `parse_intent`/`ground_intent` stay the
# only validators — exactly as with Ollama, whose `format` we also treat as
# a decoding hint rather than a trust boundary.
_SCHEMA_HINT = "отвечай строго JSON по схеме: "
MAX_DEEPSEEK_TOKENS = 400
# Tier 2 answers with SQL, not a 6-key form. A truncated JSON form fails
# `parse_intent` loudly; a truncated SELECT can still parse as valid SQL and
# quietly answer the wrong question — so SQL gets its own, larger budget.
MAX_SQL_TOKENS = 900


def provider() -> str:
    from app.services import runtime_config
    runtime_config.ensure_fresh()  # admin overlay (data/llm-runtime.json)
    return (settings.llm_provider or "ollama").strip().lower()


def _field_default(name: str):
    try:
        return type(settings).model_fields[name].default
    except Exception:                                        # pragma: no cover
        return None


def endpoint_for(name: str | None = None) -> str:
    """The base URL for `name` (default: the configured provider). An
    operator who set OP_LLM_ENDPOINT wins; deepseek falls back to its own
    public endpoint, ollama to "" (its documented kill switch)."""
    configured = (settings.llm_endpoint or "").strip()
    if configured:
        return configured
    return DEEPSEEK_ENDPOINT if (name or provider()) == "deepseek" else ""


# ── Verifier (2026-08-24): the model re-reads its own homework ──────────
# A weak model happily builds a plausible-but-wrong intent ("in MBU" dropped,
# duration rendered as a percent). Before the number is shown, ONE cheap
# check call asks: does this structured query actually answer the question?
# The prompt is built ONLY from the question and the intent's names/values
# that originated in the question — never a cell, never a row, so the
# «cells never reach the model» boundary holds. The verdict gates UI only
# (fail-open): a broken/absent verifier must never block an answer.

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "issue": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["ok"],
}

_VERIFY_MAX = 300


def build_verify_prompt(sem: dict, question: str, intent: dict,
                        residue: list[str] | None = None) -> str:
    kp = (sem.get("kpis") or {}).get(str(intent.get("kpi") or "")) or {}
    dims = [str(d) for d in (intent.get("dims") or [])]
    flt = []
    period = []
    for f in (intent.get("filters") or []):
        if not isinstance(f, dict):
            continue
        d = str(f.get("dim") or "")
        v = f.get("values") if f.get("values") is not None else f.get("value")
        (period if d in ("year", "month", "week", "day") else flt).append(
            f"{d} {f.get('op', '=')} {v!r}")
    unit = kp.get("unit")
    fmt = (f"число с единицей «{unit}»" if unit
           else ("процент (доля)" if kp.get("aggregation") == "ratio" else "число"))
    lines = [
        "Ты — контролёр аналитического запроса. Проверь, отвечает ли собранный запрос на вопрос пользователя.",
        f"Вопрос: «{question}»",
        "Собранный запрос:",
        f"- метрика: {kp.get('name') or intent.get('kpi')}"
        + (f" — {kp.get('definition')}" if kp.get("definition") else ""),
        f"- разрез: {', '.join(dims) if dims else 'итог без разреза'}",
        f"- фильтры: {', '.join(flt) if flt else 'нет'}",
        f"- период: {', '.join(period) if period else 'весь доступный'}",
        f"- формат ответа: {fmt}",
    ]
    if residue:
        lines.append("Слова " + ", ".join(f"«{w}»" for w in residue[:4])
                     + " будут автоматически сверены со значениями данных — не считай их пропущенными.")
    lines.append(
        'Ответь ТОЛЬКО JSON. Если запрос полностью и корректно отвечает на вопрос: {"ok": true}. '
        'Иначе: {"ok": false, "issue": "<что не учтено или неверно, кратко>", '
        '"question": "<ОДИН уточняющий вопрос пользователю по-русски>"}. '
        "Не пиши SQL, не проси данные, не предлагай других метрик, которых нет выше.")
    return "\n".join(lines)


def parse_verify(raw: str) -> dict:
    """Tolerant: first JSON object in the text. Anything unparsable is
    {"ok": True} — the verifier is advisory, never a blocker."""
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        return {"ok": True}
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return {"ok": True}
    if not isinstance(obj, dict) or obj.get("ok") is not False:
        return {"ok": True}
    return {"ok": False,
            "issue": str(obj.get("issue") or "")[:_VERIFY_MAX],
            "question": str(obj.get("question") or "")[:_VERIFY_MAX]}


def model_for(name: str | None = None) -> str:
    """The model id for `name`. OP_LLM_MODEL's default is an Ollama tag, so
    a deepseek deployment that never set it gets `deepseek-chat` rather than
    a 400 about an unknown model."""
    configured = (settings.llm_model or "").strip()
    if (name or provider()) == "deepseek" and (not configured or configured == _field_default("llm_model")):
        return DEEPSEEK_MODEL
    return configured


def with_schema_hint(prompt: str, fmt: dict | None) -> str:
    """The prompt the browser and the server both send to DeepSeek: the
    normal prompt plus one line naming the schema, because JSON mode does
    not enforce one. Kept here so the page (which gets the prompt from
    `/api/explore/intent/prepare`) and the server build the same string."""
    if not fmt:
        return prompt
    compact = json.dumps(fmt, ensure_ascii=False, separators=(",", ":"))
    return prompt + "\n" + _SCHEMA_HINT + compact


def call_llm(prompt: str, fmt: dict | None = None, max_tokens: int | None = None) -> str:
    """The model call every caller in the app makes. Dispatches on
    `OP_LLM_PROVIDER`; every failure mode of every provider comes back as
    `Nl2SqlError` so the routes stay thin translators.

    `max_tokens` only reaches providers that have such a knob (DeepSeek);
    Ollama's `/api/generate` has none in the body we send."""
    name = provider()
    if name == "deepseek":
        return call_deepseek(prompt, fmt, max_tokens)
    if name != "ollama":
        raise Nl2SqlError(503, f"Неизвестный LLM-провайдер: {name[:40]!r}")
    return call_ollama(prompt, fmt)


def _scrub(text: str, key: str) -> str:
    """Defence in depth: the key is only ever an Authorization header, but an
    exception string is the one place a careless library could echo it, and
    this message goes into the audit log and to the browser."""
    return text.replace(key, "***") if key else text


def call_deepseek(prompt: str, fmt: dict | None = None, max_tokens: int | None = None) -> str:
    """POST the prompt to `{endpoint}/chat/completions` (OpenAI-compatible),
    return the assistant message content. `fmt` switches on JSON mode AND
    appends the schema to the prompt text — DeepSeek validates neither, so
    `parse_intent` is still what decides whether the answer is usable.

    The key is read here and nowhere else, never logged (`OP_LLM_LOG` logs
    the prompt only) and scrubbed out of any error text."""
    key = (settings.llm_api_key or "").strip()
    if not key:
        raise Nl2SqlError(503, "LLM отключён: не настроен OP_LLM_API_KEY")
    base = endpoint_for("deepseek")
    url = base.rstrip("/") + "/chat/completions"
    text_prompt = with_schema_hint(prompt, fmt)
    if settings.llm_log:
        import logging
        logging.getLogger("onepage.nl2sql").info("prompt: %s", text_prompt)
    body = {
        "model": model_for("deepseek"),
        "messages": [{"role": "system", "content": _DEEPSEEK_SYSTEM},
                     {"role": "user", "content": text_prompt}],
        "temperature": 0,
        "max_tokens": int(max_tokens or MAX_DEEPSEEK_TOKENS),
        "stream": False,
    }
    if fmt:
        body["response_format"] = {"type": "json_object"}
    timeout = httpx.Timeout(settings.llm_timeout_s, connect=_CONNECT_TIMEOUT_S)
    try:
        resp = httpx.post(url, json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise Nl2SqlError(503, _scrub(f"LLM-сервис недоступен: {e}", key)) from e
    except (ValueError, json.JSONDecodeError) as e:
        raise Nl2SqlError(503, _scrub(f"LLM-сервис вернул не-JSON ответ: {e}", key)) from e
    text = None
    if isinstance(data, dict):
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            text = (choices[0].get("message") or {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise Nl2SqlError(503, "LLM-сервис вернул пустой ответ")
    return text


def _drop_format_support(status: int) -> None:
    global _FORMAT_SUPPORTED
    if _FORMAT_SUPPORTED:
        import logging
        logging.getLogger("onepage.nl2sql").warning(
            "Ollama rejected structured output (`format`) with HTTP %s — "
            "retrying without it and disabling it for this process "
            "(needs Ollama >= 0.5)", status)
    _FORMAT_SUPPORTED = False
