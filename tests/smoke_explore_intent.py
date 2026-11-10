"""Smoke test for the intent route (ADR-13 E4): question -> KPI/dims/filters
over a generator's frozen semantics. Pins: the prompt carries definitions
only (no inline row values, no KPI SQL), reach is the generator's, the
model's answer is validated against the semantics, never trusted.

    .venv/bin/python tests/smoke_explore_intent.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-smoke-intent-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_EXPLORE_ENABLED": "true",
    "OP_LLM_ENDPOINT": "http://127.0.0.1:1",  # never reached: call_ollama is stubbed
})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import nl2sql  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


SEM = {
    "schema_version": 1, "generator": "intent-test",
    "glossary": {"SL": "Share of the ordered quantity delivered"},
    "rules": {"reporting_start": "2026-06-01"},
    "tables": {
        "orders": {"description": "Orders", "sources": [{"file": "SL_*.xlsx", "sheet": "Orders"}],
                   "columns": {"client": {"source": "Customer", "type": "text", "role": "dim",
                                          "description": "Buyer chain (Daisy, Cornflower …)"},
                               "week": {"source": "Week", "type": "text", "role": "time"},
                               "order_qty": {"source": "Order, pcs", "type": "number", "role": "measure"},
                               "fact_qty": {"source": "Actual", "type": "number", "role": "measure"}}},
        "secret_map": {"description": "inline", "sources": [{"object": "cfg"}],
                       "columns": {"client": {"type": "text", "role": "key"}, "display": {"type": "text", "role": "attr"}},
                       "rows": [{"client": "SECRET-RAW-VALUE", "display": "Daisy"}]},
    },
    "views": {"lines": "SELECT o.*, least(order_qty, fact_qty) AS fact_clipped FROM orders o"},
    "kpis": {"sl": {"name": "Service level", "definition": "Σmin(actual,order)/Σorder", "aggregation": "ratio",
                    # Cyrillic on purpose: the question forms and the questions below
                    # exercise the Russian intent parser (stems, periods, "why" detection).
                    "question_forms": ["какой SL", "уровень сервиса"],
                    "cell_sql": "SELECT client, week, SUM(fact_clipped) AS n, SUM(order_qty) AS d FROM lines GROUP BY 1, 2"}},
}


def bundle(name, sem):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "g.main"},
         "outputs": {"html": True, "slots": []}, "engine": "auto"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("g/__init__.py", "")
        z.writestr("g/main.py", "def run(*a, **k):\n    pass\n")
        if sem is not None:
            z.writestr("semantics.json", json.dumps(sem, ensure_ascii=False))
    return buf.getvalue()


# ── pure: prompt + parse ─────────────────────────────────────────────────
# The prompt and the parser work on FROZEN semantics — the registration
# dry run is what fills each KPI's `dims` from its SQL's result columns.
from app.services import semantics as semantics_svc  # noqa: E402
SEM = semantics_svc.normalize(SEM)
assert semantics_svc.dry_run(SEM, inplace=True) == []
prompt = nl2sql.build_intent_prompt(SEM, None, "какой SL у Ромашки на 23 неделе?")
check("intent prompt names the KPI and its dims", "sl:" in prompt and "client, week" in prompt, prompt[:400])
check("intent prompt carries NO inline row values", "SECRET-RAW-VALUE" not in prompt)
check("intent prompt carries NO KPI SQL", "SUM(fact_clipped)" not in prompt and "cell_sql" not in prompt)
check("intent prompt carries the glossary", "SL: Share" in prompt)
# Dims are identifiers the model has never seen; the "dimensions" block puts
# the author's own description of each column next to its name (the word the
# user says — "warehouse", "branch"), from tables.*.columns / views.*.derived_columns.
# The prompt itself is still Russian in app/services/nl2sql.py, hence the Russian markers.
check("intent prompt describes each dim in the author's words",
      "Измерения (что значит каждое):" in prompt and "\n- client: Buyer chain" in prompt
      and "\n- week\n" in prompt, prompt[:900])
check("intent prompt lists every dim of the menu once",
      prompt.count("\n- client") == 1 and prompt.count("\n- week") == 1, prompt[:900])

good = nl2sql.parse_intent('```json\n{"kpi": "sl", "dims": ["client"], "filters": [{"dim": "week", "op": "=", "value": "23.2026"}, {"dim": "nope", "op": "=", "value": 1}, {"dim": "client", "op": "drop", "value": 1}], "order": {"by": "value", "dir": "desc"}, "limit": 5000, "explanation": "SL by chain"}\n```', SEM)
check("parse: kpi/dims kept", good["kpi"] == "sl" and good["dims"] == ["client"], good)
check("parse: unknown dim and unknown op dropped, valid filter kept", good["filters"] == [{"dim": "week", "op": "=", "value": "23.2026"}], good["filters"])
check("parse: limit clamped", good["limit"] == 1000, good["limit"])
# Ranking: the menu the model sees is narrowed by word overlap, generically.
SEM3 = json.loads(json.dumps(SEM))
SEM3["kpis"]["by_hub"] = {"name": "SL по точкам отгрузки", "definition": "SL в разрезе хаба", "aggregation": "ratio",
                          "question_forms": ["какой хаб", "SL по хабам"], "dims": ["hub"],
                          "cell_sql": "SELECT client AS hub, SUM(fact_clipped) AS n, SUM(order_qty) AS d FROM lines GROUP BY 1"}
for i in range(8):
    SEM3["kpis"][f"filler_{i}"] = {"name": f"Other {i}", "definition": "not about this", "aggregation": "sum",
                                   "cell_sql": "SELECT client, SUM(order_qty) AS value FROM lines GROUP BY 1"}
ranked = nl2sql.rank_kpis(SEM3, "у какого хаба самый высокий SL?", None)
check("rank: the KPI whose question form matches comes first", ranked[0] == "by_hub", ranked)
check("rank: the menu is capped (not the whole list of 10)", len(ranked) <= nl2sql.MAX_PROMPT_KPIS, len(ranked))
p3 = nl2sql.build_intent_prompt(SEM3, ranked, "у какого хаба самый высокий SL?")
check("rank: the prompt lists only the shortlist", "by_hub:" in p3 and "filler_7:" not in p3)

# ── residue: value-shaped words nothing consumed (2026-08-24, "in MBU") ────
res = nl2sql.residue_words("какой SL у Romashka в MBU за 23 неделю?", SEM, {"filters": []})
check("residue: the latin value-word survives, the KPI's own words do not",
      res == ["Romashka", "MBU"] or res == ["MBU", "Romashka"], res)
res = nl2sql.residue_words("какой SL в MBU?", SEM, {"filters": [{"dim": "client", "value": "MBU"}]})
check("residue: a word already in a filter is consumed", res == [], res)
res = nl2sql.residue_words("какой SL у «Магнолии»?", SEM, {"filters": []})
check("residue: a quoted cyrillic phrase is a candidate", res == ["Магнолии"], res)
check("residue: russian prose never becomes a candidate",
      nl2sql.residue_words("почему всё так плохо сегодня?", SEM, {"filters": []}) == [])

# ── verifier: prompt is names-only, verdict parse is fail-open ──────────
vp = nl2sql.build_verify_prompt(SEM, "какой SL в MBU?", {"kpi": "sl", "dims": ["client"],
     "filters": [{"dim": "week", "op": "=", "value": "23.2026"}]}, ["MBU"])
check("verify prompt: question, metric and filters are in, cells are NOT",
      "какой SL в MBU?" in vp and "Service level" in vp and "week" in vp
      and "SECRET-RAW-VALUE" not in vp and "SUM(fact_clipped)" not in vp, vp[:300])
check("verify prompt: residue words are declared as auto-checked",
      "«MBU»" in vp and "сверены со значениями" in vp)
check("verify parse: ok stays ok", nl2sql.parse_verify('{"ok": true}') == {"ok": True})
v = nl2sql.parse_verify('garbage {"ok": false, "issue": "no MBU filter", "question": "is MBU a unit?"} garbage')
check("verify parse: not-ok carries issue and ONE question",
      v["ok"] is False and v["issue"] == "no MBU filter" and v["question"] == "is MBU a unit?", v)
check("verify parse: garbage is fail-open (ok)", nl2sql.parse_verify("nothing of the kind")["ok"] is True)
check("verify parse: oversized fields are capped",
      len(nl2sql.parse_verify(json.dumps({"ok": False, "issue": "x" * 900}))["issue"]) <= 300)

# ── form-first routing: the answer without the model ─────────────────────
# Confidence rule (documented in nl2sql.match_forms): the best KPI must
# score >= FORM_MIN_SCORE and >= FORM_LEAD x the runner-up. Score of one
# form = shared content stems + 2 per stem when the whole form appears
# verbatim in the question.
m = nl2sql.match_forms(SEM3, "SL по хабам за июль?", None)
check("forms: a near-quote of one KPI's form matches confidently", m and m["kpi"] == "by_hub", m)
check("forms: the match reports the score it beat the runner-up by",
      m and m["score"] >= nl2sql.FORM_MIN_SCORE and m["score"] >= nl2sql.FORM_LEAD * m["runner_up"], m)
check("forms: a question sharing one word with two KPIs is NOT confident",
      nl2sql.match_forms(SEM3, "SL", None) is None, nl2sql.match_forms(SEM3, "SL", None))
SEM_AMB = json.loads(json.dumps(SEM3))
SEM_AMB["kpis"]["twin"] = dict(SEM_AMB["kpis"]["by_hub"], name="SL по точкам", question_forms=["какой хаб", "SL по хабам"])
check("forms: two KPIs carrying the same form -> no confident match (tie can never win)",
      nl2sql.match_forms(SEM_AMB, "SL по хабам", None) is None, nl2sql.match_forms(SEM_AMB, "SL по хабам", None))
check("forms: a question about nothing in the menu -> None",
      nl2sql.match_forms(SEM3, "кто последний раз открывал файл?", None) is None)
check("forms: `ready` narrows the candidates",
      nl2sql.match_forms(SEM3, "SL по хабам за июль?", ["sl"]) is None
      or nl2sql.match_forms(SEM3, "SL по хабам за июль?", ["sl"])["kpi"] == "sl")

# Grounding: the model may not invent time values the question never named.
SEM2 = json.loads(json.dumps(SEM))
SEM2["kpis"]["sl"]["cell_sql"] = "SELECT client, CAST(split_part(week, '.', 2) AS INTEGER) AS year, 7 AS month, CAST(split_part(week, '.', 1) AS INTEGER) AS week, SUM(fact_clipped) AS n, SUM(order_qty) AS d FROM lines GROUP BY 1, 2, 3, 4"
SEM2["kpis"]["sl"].pop("dims", None)   # re-derive from the new SQL
assert semantics_svc.dry_run(SEM2, inplace=True) == []
assert SEM2["kpis"]["sl"]["dims"] == ["client", "year", "month", "week"], SEM2["kpis"]["sl"]["dims"]
raw_i = nl2sql.parse_intent('{"kpi": "sl", "dims": ["client", "year", "month"], "filters": [{"dim": "year", "op": "=", "value": 2023}, {"dim": "month", "op": "=", "value": 7}, {"dim": "week", "op": "=", "value": 46}], "limit": 5}', SEM2)
g = nl2sql.ground_intent("у какого клиента в июле 2026 самый высокий SL на 27 неделе?", raw_i)
fl = {(f["dim"], f.get("value")) for f in g["filters"]}
check("ground: hallucinated year 2023 replaced by the question's 2026", ("year", 2026) in fl and ("year", 2023) not in fl, g["filters"])
check("ground: week 46 replaced by the question's 27", ("week", 27) in fl, g["filters"])
check("ground: named month kept", ("month", 7) in fl, g["filters"])
check("ground: pinned dims leave the grouping (client stays)", g["dims"] == ["client"], g["dims"])
g2 = nl2sql.ground_intent("SL по клиентам", nl2sql.parse_intent('{"kpi": "sl", "dims": ["client"], "filters": [{"dim": "year", "op": "=", "value": 2025}], "limit": 5}', SEM2))
check("ground: a year the question never named is dropped", g2["filters"] == [], g2["filters"])
g3 = nl2sql.ground_intent("SL по клиентам за июль", nl2sql.parse_intent('{"kpi": "sl", "dims": ["client"], "filters": [], "limit": 5}', SEM2))
check("ground: a month the model forgot is added from the question", g3["filters"] == [{"dim": "month", "op": "=", "value": 7}], g3["filters"])
check("ground: internal key never leaks", "_allowed_dims" not in g3)

# ── follow-ups (phase 3): prev_intent, the pure half ─────────────────────
# The follow-up "and by week?" names no period at all. The honest reading is "the same
# period as the answer I am looking at" — so the OPEN answer's time filters
# are inherited verbatim. A question that DOES name a period overrides them:
# inherit-or-be-told, never merge.
PREV = {"kpi": "sl", "dims": ["client"],
        "filters": [{"dim": "month", "op": "=", "value": 7},
                    {"dim": "year", "op": "=", "value": 2026},
                    {"dim": "client", "op": "=", "value": "Daisy"}]}
gi = nl2sql.ground_intent(
    "а по неделям?",
    nl2sql.parse_intent('{"kpi": "sl", "dims": ["week"], "filters": [], "limit": 10}', SEM2),
    prev_intent=PREV)
check("ground(prev): a question naming no period inherits the previous one",
      sorted((f["dim"], f["value"]) for f in gi["filters"]) == [("month", 7), ("year", 2026)],
      gi["filters"])
check("ground(prev): only TIME filters are inherited — a dim filter is not a period",
      all(f["dim"] != "client" for f in gi["filters"]), gi["filters"])
check("ground(prev): the new cut survives the inheritance", gi["dims"] == ["week"], gi["dims"])
gi2 = nl2sql.ground_intent(
    "а в июне?",
    nl2sql.parse_intent('{"kpi": "sl", "dims": ["client"], "filters": [], "limit": 10}', SEM2),
    prev_intent=PREV)
check("ground(prev): a question that NAMES a period overrides, never merges",
      sorted((f["dim"], f["value"]) for f in gi2["filters"]) == [("month", 6)], gi2["filters"])
gi3 = nl2sql.ground_intent(
    "а по неделям?",
    nl2sql.parse_intent('{"kpi": "sl", "dims": ["week"], "filters": [], "limit": 10}', SEM2),
    prev_intent={"kpi": "sl", "dims": [], "filters": [{"dim": "nope", "op": "=", "value": 1}]})
check("ground(prev): a dim this KPI does not have is never inherited",
      gi3["filters"] == [], gi3["filters"])
gi4 = nl2sql.ground_intent(
    "на какой неделе был самый низкий SL у василька",
    nl2sql.parse_intent('{"kpi": "sl", "dims": ["week"], "filters": [], "limit": 1}', SEM2),
    prev_intent={"kpi": "sl", "dims": [], "filters": [{"dim": "week", "op": "=", "value": 34}, {"dim": "year", "op": "=", "value": 2026}]})
check("ground(prev): a period on the dim the question GROUPS BY is not inherited (prod 2026-08-28: week 34 pinned the week cut away)",
      gi4["dims"] == ["week"] and all(f["dim"] != "week" for f in gi4["filters"])
      and any(f["dim"] == "year" and f["value"] == 2026 for f in gi4["filters"]), (gi4["dims"], gi4["filters"]))
check("ground(prev): without a prev_intent nothing changes",
      nl2sql.ground_intent("а по неделям?", nl2sql.parse_intent(
          '{"kpi": "sl", "dims": ["week"], "filters": [], "limit": 10}', SEM2))["filters"] == [])

# has_metric_words: the guard that decides whether the previous metric may be
# pinned. Same vocabulary `match_forms` scores against.
check("metric words: a question quoting a KPI's own form names a metric",
      nl2sql.has_metric_words(SEM2, "какой SL по неделям?") is True)
check("metric words: a bare follow-up names none",
      nl2sql.has_metric_words(SEM2, "а по неделям?") is False,
      nl2sql.has_metric_words(SEM2, "а по неделям?"))

# prev_intent_line: NAMES ONLY. A filter's value is a cell read off the user's
# folder and may never reach a model (docs/LLM_PROVIDERS.md data boundary).
line = nl2sql.prev_intent_line(SEM2, PREV)
check("prev line: names the previous metric and its cut",
      "Service level" in line and "sl" in line and "client" in line, line)
check("prev line: names WHICH time dims carried a period",
      "month" in line and "year" in line, line)
check("prev line: carries NO values — not the month number, not the year, not the client",
      "Daisy" not in line and " 7" not in line and "2026" not in line, line)
check("prev line: no previous answer -> no line", nl2sql.prev_intent_line(SEM2, None) == "")

# form_intent: no model, no invented anything — KPI + the question's own period.
fi = nl2sql.form_intent(SEM2, "sl", "SL по клиентам за июль 2026")
check("form_intent: the matched KPI, no dims (the panel lets the user pick)",
      fi["kpi"] == "sl" and fi["dims"] == [], fi)
check("form_intent: the period comes from the question only",
      sorted((f["dim"], f["value"]) for f in fi["filters"]) == [("month", 7), ("year", 2026)], fi["filters"])
check("form_intent: biggest first, top-10",
      fi["order"] == {"by": "value", "dir": "desc"} and fi["limit"] == nl2sql.FORM_LIMIT, fi)
check("form_intent: internal key never leaks", "_allowed_dims" not in fi)

# ── "why" questions: classification, stripping, period arithmetic ──────
# The defect this guards: "why did SL drop in January 2026?" used to route to
# the KPI path, where the superlative rule added ORDER BY value DESC LIMIT 1
# to a one-row aggregate. There was no classifier for reason-questions at all.
for q, cls in (
        ("почему в январе 2026 SL упал?", "why"),
        ("Почему упал SL?", "why"),
        ("ПОЧЕМУ", "why"),
        ("зачем это", "why"),
        ("из-за чего провал в июле?", "why"),
        ("отчего SL упал", "why"),
        ("что случилось с SL в январе?", "why"),
        ("что произошло на 27 неделе?", "why"),
        ("а почему SL упал?", "why"),               # "why" inside the first 3 words
        ("SL: почему так?", "why"),
        ("какой SL за январь?", "kpi"),
        ("у какого хаба самый высокий SL?", "kpi"),
        ("покажи заказы, и непонятно почему они такие", "kpi"),  # "why" too deep to be the verb
):
    check(f"classify: {q!r} -> {cls}", nl2sql.classify_question(q) == cls, nl2sql.classify_question(q))

check("strip_why: the interrogative goes, the metric and the period stay",
      nl2sql.strip_why("почему в январе 2026 SL упал?") == "в январе 2026 SL упал",
      nl2sql.strip_why("почему в январе 2026 SL упал?"))
check("strip_why: a KPI question is left alone",
      nl2sql.strip_why("какой SL за январь?") == "какой SL за январь",
      nl2sql.strip_why("какой SL за январь?"))

for label, filters, expect in (
        ("Jan -> Dec of the previous year",
         [{"dim": "year", "op": "=", "value": 2026}, {"dim": "month", "op": "=", "value": 1}],
         [{"dim": "year", "op": "=", "value": 2025}, {"dim": "month", "op": "=", "value": 12}]),
        ("mid-year month just steps back",
         [{"dim": "year", "op": "=", "value": 2026}, {"dim": "month", "op": "=", "value": 7}],
         [{"dim": "year", "op": "=", "value": 2026}, {"dim": "month", "op": "=", "value": 6}]),
        ("week 1 -> week 52 of the previous year",
         [{"dim": "year", "op": "=", "value": 2026}, {"dim": "week", "op": "=", "value": 1}],
         [{"dim": "year", "op": "=", "value": 2025}, {"dim": "week", "op": "=", "value": 52}]),
        ("week is finer than month, so it is the one that steps back",
         [{"dim": "month", "op": "=", "value": 7}, {"dim": "week", "op": "=", "value": 27}],
         [{"dim": "month", "op": "=", "value": 7}, {"dim": "week", "op": "=", "value": 26}]),
        ("day as an ISO date -> the previous calendar day, month/year carried",
         [{"dim": "year", "op": "=", "value": 2026}, {"dim": "month", "op": "=", "value": 1},
          {"dim": "day", "op": "=", "value": "2026-01-01"}],
         [{"dim": "year", "op": "=", "value": 2025}, {"dim": "month", "op": "=", "value": 12},
          {"dim": "day", "op": "=", "value": "2025-12-31"}]),
        ("day as a number with a year+month -> the previous calendar day",
         [{"dim": "year", "op": "=", "value": 2026}, {"dim": "month", "op": "=", "value": 3},
          {"dim": "day", "op": "=", "value": 1}],
         [{"dim": "year", "op": "=", "value": 2026}, {"dim": "month", "op": "=", "value": 2},
          {"dim": "day", "op": "=", "value": 28}]),
        ("year only -> the previous year",
         [{"dim": "year", "op": "=", "value": 2026}],
         [{"dim": "year", "op": "=", "value": 2025}]),
        ("non-time filters ride along untouched",
         [{"dim": "month", "op": "=", "value": 7}, {"dim": "client", "op": "=", "value": "Daisy"}],
         [{"dim": "month", "op": "=", "value": 6}, {"dim": "client", "op": "=", "value": "Daisy"}]),
):
    check(f"shift_period: {label}", nl2sql.shift_period(filters) == expect, nl2sql.shift_period(filters))

check("shift_period: no time filter at all -> None",
      nl2sql.shift_period([{"dim": "client", "op": "=", "value": "Daisy"}]) is None,
      nl2sql.shift_period([{"dim": "client", "op": "=", "value": "Daisy"}]))
check("shift_period: empty filters -> None", nl2sql.shift_period([]) is None)
check("shift_period: an `in` period has no single predecessor -> None",
      nl2sql.shift_period([{"dim": "month", "op": "in", "values": [5, 6, 7]}]) is None,
      nl2sql.shift_period([{"dim": "month", "op": "in", "values": [5, 6, 7]}]))
check("shift_period: does not mutate its input",
      (lambda f: (nl2sql.shift_period(f), f)[1] == [{"dim": "month", "op": "=", "value": 7}])(
          [{"dim": "month", "op": "=", "value": 7}]))

why_i = nl2sql.why_intent(SEM2, "sl", nl2sql.strip_why("почему в январе 2026 упал SL?"))
check("why_intent: the KPI, no grouping, and only the question's own period",
      why_i["kpi"] == "sl" and why_i["dims"] == []
      and sorted((f["dim"], f["value"]) for f in why_i["filters"]) == [("month", 1), ("year", 2026)],
      why_i)
check("why_intent: NO order and NO limit — the superlative rule is exactly what broke this",
      "order" not in why_i and "limit" not in why_i, why_i)
why_m = nl2sql.why_intent_from_raw(
    '{"kpi": "sl", "dims": ["client"], "order": {"by": "value", "dir": "desc"}, "limit": 1}',
    SEM2, "в январе 2026 упал SL")
check("why_intent_from_raw: the model contributes the METRIC and nothing else",
      why_m["kpi"] == "sl" and why_m["dims"] == [] and "order" not in why_m and "limit" not in why_m,
      why_m)

# ── structured outputs: the schema the model is constrained to ────────────
schema = nl2sql.build_intent_schema(SEM3, ["by_hub", "sl"])
check("schema: kpi enum is exactly the shortlist (+ null)",
      schema["properties"]["kpi"]["enum"] == ["by_hub", "sl", None], schema["properties"]["kpi"])
check("schema: dims enum is the union of those KPIs' dims",
      schema["properties"]["dims"]["items"]["enum"] == sorted(set(SEM3["kpis"]["by_hub"]["dims"]) | set(SEM3["kpis"]["sl"]["dims"])),
      schema["properties"]["dims"]["items"])
check("schema: filter dim enum matches, op enum is INTENT_OPS",
      schema["properties"]["filters"]["items"]["properties"]["dim"]["enum"] == schema["properties"]["dims"]["items"]["enum"]
      and schema["properties"]["filters"]["items"]["properties"]["op"]["enum"] == list(nl2sql.INTENT_OPS),
      schema["properties"]["filters"]["items"]["properties"])
check("schema: order.by covers the measures and the dims",
      schema["properties"]["order"]["properties"]["by"]["enum"][:3] == ["value", "n", "d"],
      schema["properties"]["order"]["properties"]["by"])
check("schema: limit is a bounded integer",
      schema["properties"]["limit"] == {"type": "integer", "minimum": 1, "maximum": 1000}, schema["properties"]["limit"])
check("schema: a KPI outside the shortlist is unrepresentable",
      "filler_3" not in json.dumps(schema, ensure_ascii=False))

# ── fixed_kpi: the one-shot form path ────────────────────────────────────
# The form matcher already decided WHICH metric, deterministically. The model
# is then asked for the CUT only — so the menu is one KPI, the enum is one
# key with no `null`, and the dims enum is exactly that KPI's own dims.
fixed_schema = nl2sql.build_intent_schema(SEM3, ["by_hub"], fixed_kpi="by_hub")
check("fixed_kpi: the kpi enum is exactly the one metric, with no null option",
      fixed_schema["properties"]["kpi"] == {"type": "string", "enum": ["by_hub"]},
      fixed_schema["properties"]["kpi"])
check("fixed_kpi: the dims enum is that KPI's own dims and nothing else",
      fixed_schema["properties"]["dims"]["items"]["enum"] == sorted(SEM3["kpis"]["by_hub"]["dims"]),
      fixed_schema["properties"]["dims"]["items"])
check("fixed_kpi: the filter dim enum is pinned the same way",
      fixed_schema["properties"]["filters"]["items"]["properties"]["dim"]["enum"]
      == sorted(SEM3["kpis"]["by_hub"]["dims"]),
      fixed_schema["properties"]["filters"]["items"]["properties"]["dim"])
fixed_prompt = nl2sql.build_intent_prompt(SEM3, ["by_hub"], "а по хабам за июль?", fixed_kpi="by_hub")
check("fixed_kpi: the prompt says the metric is already chosen",
      "Метрика уже выбрана" in fixed_prompt and "by_hub" in fixed_prompt, fixed_prompt[:200])
check("fixed_kpi: the prompt offers NO other metric to drift to",
      "filler_3" not in fixed_prompt and "\n- sl:" not in fixed_prompt, fixed_prompt[:400])
check("fixed_kpi: the prompt still carries the glossary", "SL: Share" in fixed_prompt)
check("fixed_kpi: the prompt describes the pinned KPI's dims only",
      "Измерения (что значит каждое):" in fixed_prompt and "\n- hub" in fixed_prompt
      and "\n- client" not in fixed_prompt, fixed_prompt[:600])
# A model that ignores the pin — answers a different KPI, or declines — must
# not be able to undo a deterministic match.
pinned = nl2sql.parse_intent('{"kpi": "sl", "dims": ["hub"], "filters": [], "limit": 5}',
                             SEM3, fixed_kpi="by_hub")
check("fixed_kpi: parse_intent pins the metric whatever the model wrote",
      pinned["kpi"] == "by_hub" and pinned["dims"] == ["hub"], pinned)
declined = nl2sql.parse_intent('{"kpi": null, "explanation": "no idea"}', SEM3, fixed_kpi="by_hub")
check("fixed_kpi: a model that declines cannot turn a confident match into a 422",
      declined["kpi"] == "by_hub", declined)
try:
    nl2sql.parse_intent('{"kpi": "sl"}', SEM3, fixed_kpi="not_a_kpi")
    check("fixed_kpi: a key the semantics do not have is ignored, not trusted", True)
except nl2sql.Nl2SqlError:
    check("fixed_kpi: a key the semantics do not have is ignored, not trusted", False)

# ── tier 2: the semantic-schema prompt ───────────────────────────────────
# The fall-through for a question no KPI answers: the report's own tables and
# views, their columns, types and Russian descriptions — and one SELECT back.
STATUS = {"tables": {"orders": "bound", "secret_map": "inline"}, "views": {"lines": "ok"}}
sem_prompt = nl2sql.build_semantic_sql_prompt(SEM, STATUS, "how many rows have no customer?")
check("tier2 prompt: names the views and the tables that are actually bound",
      '"lines"' in sem_prompt and '"orders"' in sem_prompt, sem_prompt[:300])
check("tier2 prompt: lists the view's REAL columns (resolved through its SQL, not just declared)",
      '"fact_clipped"' in sem_prompt and '"client"' in sem_prompt and '"order_qty"' in sem_prompt,
      sem_prompt[:800])
check("tier2 prompt: every column carries its type",
      '"order_qty" (number)' in sem_prompt, sem_prompt[:900])
check("tier2 prompt: carries the author's Russian descriptions",
      "Orders" in sem_prompt, sem_prompt[:600])
check("tier2 prompt: carries the glossary and the rules text",
      "SL: Share" in sem_prompt and "reporting_start" in sem_prompt or "2026-06-01" in sem_prompt,
      sem_prompt[-400:])
check("tier2 prompt: asks for ONE SELECT in DuckDB with the quoting rule",
      "SELECT" in sem_prompt and "DuckDB" in sem_prompt and "двойных кавычках" in sem_prompt,
      sem_prompt[:400])
# THE boundary, pinned: an inline table's rows are IN the semantics and must
# still never reach the prompt.
check("tier2 prompt: carries NO cell values — not even an inline table's own rows",
      "SECRET-RAW-VALUE" not in sem_prompt, sem_prompt[:900])
check("tier2 prompt: carries NO KPI SQL", "SUM(fact_clipped)" not in sem_prompt)
# A relation the folder did not produce must not be offered: SQL naming it
# would fail in the engine, and the model has no way to know that.
NARROW = {"tables": {"orders": "unbound", "secret_map": "inline"}, "views": {"lines": "error"}}
narrow_prompt = nl2sql.build_semantic_sql_prompt(SEM, NARROW, "q")
check("tier2 prompt: an unbound table and a failed view are not offered to the model",
      '"orders"' not in narrow_prompt and '"lines"' not in narrow_prompt
      and '"secret_map"' in narrow_prompt, narrow_prompt[:500])
check("tier2: semantic_relations reports what was offered",
      nl2sql.semantic_relations(SEM, STATUS) == ["lines", "orders", "secret_map"],
      nl2sql.semantic_relations(SEM, STATUS))


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"response": '{"kpi": "sl"}'}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx as _h
            raise _h.HTTPStatusError("boom", request=None, response=None)


posts = []
import httpx as _httpx  # noqa: E402
_orig_post = _httpx.post
nl2sql.settings.llm_endpoint = "http://ollama.test:11434"
try:
    _httpx.post = lambda url, **kw: (posts.append(dict(kw.get("json") or {})), _Resp())[1]
    nl2sql.call_ollama("p", schema)
    check("call_ollama: sends the schema as Ollama's `format`", posts and posts[-1].get("format") == schema, posts[-1:])
    nl2sql.call_ollama("p")
    check("call_ollama: no `format` key when no schema is given", "format" not in posts[-1], posts[-1])

    # An Ollama older than 0.5 answers 4xx to `format`: retry once without it
    # and stop sending it — the operator's box must not pay a wasted round
    # trip per question forever.
    posts.clear()
    calls = {"n": 0}

    def _reject_format(url, **kw):
        calls["n"] += 1
        posts.append(dict(kw.get("json") or {}))
        return _Resp(400 if "format" in (kw.get("json") or {}) else 200)
    _httpx.post = _reject_format
    out = nl2sql.call_ollama("p", schema)
    check("call_ollama: a 4xx on `format` is retried once without it",
          calls["n"] == 2 and "format" in posts[0] and "format" not in posts[1] and out, [list(p) for p in posts])
    posts.clear()
    nl2sql.call_ollama("p", schema)
    check("call_ollama: after that rejection `format` is not sent again",
          len(posts) == 1 and "format" not in posts[0], posts)
finally:
    _httpx.post = _orig_post
    nl2sql._FORMAT_SUPPORTED = True
    nl2sql.settings.llm_endpoint = "http://127.0.0.1:1"

# ── DeepSeek provider (docs/LLM_PROVIDERS.md) ─────────────────────────────
# The external option: an OpenAI-compatible POST with a bearer key. JSON mode
# is a MODE, not schema enforcement, so the schema must also be in the prompt
# text and parse_intent stays the validator.
KEY = "sk-test-not-a-real-key"


class _DSResp(_Resp):
    def __init__(self, status=200, content='{"kpi": "sl"}'):
        super().__init__(status, {"choices": [{"message": {"content": content}}]})


calls = []
nl2sql.settings.llm_provider = "deepseek"
nl2sql.settings.llm_api_key = KEY
# The env in this file pins OP_LLM_ENDPOINT for the Ollama tests; a deepseek
# deployment normally sets nothing and must still get a usable endpoint.
nl2sql.settings.llm_endpoint = ""
try:
    check("provider: deepseek defaults to its own endpoint and model when nothing is set",
          nl2sql.endpoint_for() == "https://api.deepseek.com" and nl2sql.model_for() == "deepseek-chat",
          f"{nl2sql.endpoint_for()} {nl2sql.model_for()}")
    _httpx.post = lambda url, **kw: (calls.append({"url": url, **kw}), _DSResp())[1]
    out = nl2sql.call_llm("THE PROMPT", schema)
    c = calls[-1]
    check("deepseek: posts to {endpoint}/chat/completions", c["url"] == "https://api.deepseek.com/chat/completions", c["url"])
    check("deepseek: bearer key in the Authorization header",
          c["headers"]["Authorization"] == "Bearer " + KEY, list(c["headers"]))
    check("deepseek: system instruction + the prompt as the user message",
          [m["role"] for m in c["json"]["messages"]] == ["system", "user"]
          and c["json"]["messages"][1]["content"].startswith("THE PROMPT"), c["json"]["messages"])
    check("deepseek: temperature 0 and a bounded max_tokens",
          c["json"]["temperature"] == 0 and c["json"]["max_tokens"] == nl2sql.MAX_DEEPSEEK_TOKENS, c["json"])
    check("deepseek: JSON mode when a schema is given",
          c["json"]["response_format"] == {"type": "json_object"}, c["json"].get("response_format"))
    check("deepseek: the schema rides in the PROMPT (JSON mode enforces none of it)",
          "отвечай строго JSON по схеме:" in c["json"]["messages"][1]["content"]
          and '"by_hub"' in c["json"]["messages"][1]["content"], c["json"]["messages"][1]["content"][-200:])
    check("deepseek: the model name is what call_llm reports", c["json"]["model"] == "deepseek-chat", c["json"]["model"])
    check("deepseek: the answer is the message content", out == '{"kpi": "sl"}', out)
    calls.clear()
    nl2sql.call_llm("THE PROMPT")
    check("deepseek: no response_format and no schema line without a schema",
          "response_format" not in calls[-1]["json"]
          and "схеме" not in calls[-1]["json"]["messages"][1]["content"], calls[-1]["json"])

    # Every failure is a 503 with a Russian message — and never the key.
    def _boom(url, **kw):
        raise _httpx.ConnectError(f"connection refused (key {KEY})")
    _httpx.post = _boom
    try:
        nl2sql.call_llm("p", schema)
        check("deepseek: a network failure maps to 503", False)
    except nl2sql.Nl2SqlError as e:
        check("deepseek: a network failure maps to 503", e.status_code == 503, e.message)
        check("deepseek: the key is never in the error message", KEY not in e.message, e.message)
    _httpx.post = lambda url, **kw: _DSResp(500)
    try:
        nl2sql.call_llm("p")
        check("deepseek: a 5xx maps to 503", False)
    except nl2sql.Nl2SqlError as e:
        check("deepseek: a 5xx maps to 503", e.status_code == 503, e.message)
    _httpx.post = lambda url, **kw: _DSResp(200, "")
    try:
        nl2sql.call_llm("p")
        check("deepseek: an empty answer maps to 503", False)
    except nl2sql.Nl2SqlError as e:
        check("deepseek: an empty answer maps to 503", e.status_code == 503, e.message)
    nl2sql.settings.llm_api_key = ""
    try:
        nl2sql.call_llm("p")
        check("deepseek: no key configured -> 503, not a call", False)
    except nl2sql.Nl2SqlError as e:
        check("deepseek: no key configured -> 503, not a call",
              e.status_code == 503 and "OP_LLM_API_KEY" in e.message, e.message)
finally:
    _httpx.post = _orig_post
    nl2sql.settings.llm_provider = "ollama"
    nl2sql.settings.llm_api_key = ""
    nl2sql.settings.llm_endpoint = "http://127.0.0.1:1"

for raw, what in (("no json here", "no JSON"), ('{"kpi": "zzz"}', "unknown kpi"), ('{"kpi": null, "explanation": "not about metrics"}', "null kpi")):
    try:
        nl2sql.parse_intent(raw, SEM)
        check(f"parse rejects {what}", False)
    except nl2sql.Nl2SqlError as e:
        check(f"parse rejects {what} -> 422", e.status_code == 422, e.message)

# ── route ────────────────────────────────────────────────────────────────
with TestClient(app) as c:
    r = c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
    assert r.status_code == 302
    r = c.post("/api/admin/generators", files={"bundle": ("b.zip", bundle("intent-test", SEM), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Intent", "generator_id": gid})
    assert r.status_code == 201, r.text[:300]
    # Same report, but with year/month/week broken out as dims — the shape a
    # "why ... in January 2026" question needs to ground a period at all.
    SEM_WHY = dict(SEM2, generator="why-test")
    r = c.post("/api/admin/generators", files={"bundle": ("b.zip", bundle("why-test", SEM_WHY), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid_why = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Why", "generator_id": gid_why})
    assert r.status_code == 201, r.text[:300]
    r = c.post("/api/admin/generators", files={"bundle": ("b.zip", bundle("no-sem", None), "application/zip")})
    gid_nosem = r.json()["id"]
    c.post("/api/dashboards/generated", json={"title": "NoSem", "generator_id": gid_nosem})

    captured = {}
    orig = nl2sql.call_ollama
    orig_llm = nl2sql.call_llm

    def fake(prompt, fmt=None):
        captured["prompt"], captured["fmt"] = prompt, fmt
        return '{"kpi": "sl", "dims": ["client"], "filters": [], "limit": 10, "explanation": "service level by chain"}'

    # The form path must not touch the model at all: this stub explodes.
    def never(prompt, fmt=None):
        raise AssertionError("the model was called on the form path")
    nl2sql.call_ollama = never
    try:
        r = c.post("/api/explore/intent", json={"question": "какой SL по сетям?", "generator_id": gid})
        d = r.json() if r.status_code == 200 else {}
        check("intent route: a question quoting a question_form is answered WITHOUT the model",
              r.status_code == 200 and d.get("source") == "form" and d["intent"]["kpi"] == "sl",
              f"{r.status_code} {r.text[:200]}")
        check("intent route: the form answer carries no dims and no model name",
              d.get("intent", {}).get("dims") == [] and d.get("model") is None, r.text[:200])
        check("intent route: the shortlist is reported either way", d.get("shortlist") == ["sl"], d.get("shortlist"))
    finally:
        nl2sql.call_ollama = orig
    nl2sql.call_ollama = fake
    try:
        # No form matches this one, so the model answers — the path the
        # structured-output schema exists for.
        r = c.post("/api/explore/intent", json={"question": "сколько всего строк осталось?", "generator_id": gid})
        check("intent route: 200 with validated intent", r.status_code == 200 and r.json()["intent"]["kpi"] == "sl", f"{r.status_code} {r.text[:200]}")
        check("intent route: source=model on the model path", r.json().get("source") == "model", r.text[:200])
        check("intent route: explanation surfaced", "service" in (r.json().get("explanation") or ""))
        check("intent route: prompt had no row values", "SECRET-RAW-VALUE" not in captured.get("prompt", "x"))
        check("intent route: the model was constrained to the shortlist's KPI keys",
              (captured.get("fmt") or {}).get("properties", {}).get("kpi", {}).get("enum") == ["sl", None],
              captured.get("fmt"))
        r = c.post("/api/explore/intent", json={"question": "q", "generator_id": gid_nosem})
        check("intent route: generator without semantics -> 404", r.status_code == 404, r.status_code)
        r = c.post("/api/explore/intent", json={"question": "q", "generator_id": 99999})
        check("intent route: unreachable generator -> 404", r.status_code == 404, r.status_code)

        nl2sql.call_ollama = lambda p, fmt=None: "I cannot help with that"
        r = c.post("/api/explore/intent", json={"question": "q", "generator_id": gid})
        check("intent route: non-JSON answer -> 422 with a human detail", r.status_code == 422 and "JSON" in r.text, f"{r.status_code} {r.text[:160]}")
        nl2sql.call_ollama = fake
        r = c.post("/api/explore/intent", json={"question": "q", "generator_id": gid, "ready_kpis": ["sl"]})
        check("intent route: ready_kpis narrows the prompt to those KPIs", r.status_code == 200 and "sl:" in captured["prompt"])
    finally:
        nl2sql.call_ollama = orig

    # ── browser path: prepare (no model) / validate (the page's answer) ───
    # The same pre-model work as /intent, handed to a page whose browser can
    # reach the provider even though this server cannot.
    nl2sql.call_ollama = never
    nl2sql.call_llm = never
    try:
        r = c.post("/api/explore/intent/prepare", json={"question": "какой SL по сетям?", "generator_id": gid})
        d = r.json() if r.status_code == 200 else {}
        check("prepare: a question quoting a question_form is answered as a FORM, no model, no prompt",
              r.status_code == 200 and d.get("source") == "form" and d["intent"]["kpi"] == "sl" and "prompt" not in d,
              f"{r.status_code} {r.text[:200]}")
        r = c.post("/api/explore/intent/prepare", json={"question": "сколько всего строк осталось?", "generator_id": gid})
        d = r.json() if r.status_code == 200 else {}
        check("prepare: the model path returns the prompt, the schema and the shortlist",
              r.status_code == 200 and d.get("source") == "model" and "Метрики:" in (d.get("prompt") or "")
              and (d.get("schema") or {}).get("properties", {}).get("kpi", {}).get("enum") == ["sl", None]
              and d.get("shortlist") == ["sl"], f"{r.status_code} {r.text[:200]}")
        check("prepare: the payload carries NO cell values (the whole point of the boundary)",
              "SECRET-RAW-VALUE" not in r.text, r.text[:200])
        check("prepare: and no KPI SQL", "SUM(fact_clipped)" not in r.text)
        r = c.post("/api/explore/intent/prepare", json={"question": "q", "generator_id": 99999})
        check("prepare: unreachable generator -> 404", r.status_code == 404, r.status_code)

        raw = '{"kpi": "sl", "dims": ["client"], "filters": [{"dim": "week", "op": "=", "value": "23.2026"}], "limit": 7, "explanation": "SL by chain"}'
        r = c.post("/api/explore/intent/validate", json={"question": "какой SL?", "generator_id": gid, "raw": raw})
        d = r.json() if r.status_code == 200 else {}
        check("validate: good model text -> validated intent, source=model",
              r.status_code == 200 and d.get("source") == "model" and d["intent"]["kpi"] == "sl"
              and d["intent"]["dims"] == ["client"] and d["intent"]["limit"] == 7,
              f"{r.status_code} {r.text[:200]}")
        check("validate: internal key never leaks", "_allowed_dims" not in r.text)
        r = c.post("/api/explore/intent/validate", json={"question": "q", "generator_id": gid, "raw": "I cannot help with that"})
        check("validate: garbage -> 422 with a human detail", r.status_code == 422 and "JSON" in r.text, f"{r.status_code} {r.text[:160]}")
        r = c.post("/api/explore/intent/validate", json={"question": "q", "generator_id": gid, "raw": '{"kpi": "zzz"}'})
        check("validate: a KPI the semantics do not have -> 422", r.status_code == 422, r.status_code)
        r = c.post("/api/explore/intent/validate", json={"question": "q", "generator_id": 99999, "raw": raw})
        check("validate: unreachable generator -> 404", r.status_code == 404, r.status_code)

        # ── fixed_kpi over the wire: prepare must NOT re-run the form
        #    matcher (the metric is already settled) and must pin the menu.
        r = c.post("/api/explore/intent/prepare",
                   json={"question": "какой SL по сетям?", "generator_id": gid, "fixed_kpi": "sl"})
        d = r.json() if r.status_code == 200 else {}
        check("prepare(fixed_kpi): answers with a PROMPT, not a form — the metric is already chosen",
              r.status_code == 200 and d.get("source") == "model" and d.get("fixed_kpi") == "sl"
              and "Метрика уже выбрана" in (d.get("prompt") or ""), f"{r.status_code} {r.text[:200]}")
        check("prepare(fixed_kpi): the shortlist is exactly that one KPI",
              d.get("shortlist") == ["sl"], d.get("shortlist"))
        check("prepare(fixed_kpi): the schema's kpi enum is that one key, no null",
              (d.get("schema") or {}).get("properties", {}).get("kpi") == {"type": "string", "enum": ["sl"]},
              (d.get("schema") or {}).get("properties", {}).get("kpi"))
        check("prepare(fixed_kpi): the dims enum is that KPI's own dims",
              (d.get("schema") or {}).get("properties", {}).get("dims", {}).get("items", {}).get("enum")
              == sorted(SEM["kpis"]["sl"]["dims"]),
              (d.get("schema") or {}).get("properties", {}).get("dims"))
        check("prepare(fixed_kpi): still no cell values", "SECRET-RAW-VALUE" not in r.text)
        # An unknown / unready key is ignored, not obeyed: the route falls back
        # to the normal choosing path rather than trusting the browser.
        r = c.post("/api/explore/intent/prepare",
                   json={"question": "какой SL по сетям?", "generator_id": gid, "fixed_kpi": "made_up"})
        check("prepare(fixed_kpi): a key the semantics do not have is ignored (form path resumes)",
              r.status_code == 200 and r.json().get("source") == "form", r.text[:200])
        r = c.post("/api/explore/intent/prepare",
                   json={"question": "какой SL по сетям?", "generator_id": gid,
                         "fixed_kpi": "sl", "ready_kpis": ["some_other_kpi"]})
        check("prepare(fixed_kpi): a KPI outside the folder's ready list is not honoured",
              r.status_code == 200 and r.json().get("fixed_kpi") is None
              and "Метрика уже выбрана" not in r.text, r.text[:200])
        # validate honours the pin the same way
        r = c.post("/api/explore/intent/validate",
                   json={"question": "q", "generator_id": gid, "fixed_kpi": "sl",
                         "raw": '{"kpi": null, "dims": ["client"], "filters": [], "limit": 3}'})
        d = r.json() if r.status_code == 200 else {}
        check("validate(fixed_kpi): a declining model cannot undo a deterministic match",
              r.status_code == 200 and d.get("intent", {}).get("kpi") == "sl"
              and d["intent"]["dims"] == ["client"], f"{r.status_code} {r.text[:200]}")
    finally:
        nl2sql.call_ollama = orig
        nl2sql.call_llm = orig_llm

    # ── follow-ups over the wire (phase 3) ────────────────────────────────
    # The chat's thread lives in the browser. What crosses is ONE intent — the
    # answer the user is looking at — so the follow-up "and by week?" can be read at all.
    fu_prompts = []

    def fu_model(prompt, fmt=None, max_tokens=None):
        fu_prompts.append(prompt)
        # A model that drifts to another metric must not be able to undo the
        # pin the previous answer earned.
        return ('{"kpi": "by_hub", "dims": ["week"], "filters": [], '
                '"limit": 10, "explanation": "by week"}')
    PREV_BODY = {"kpi": "sl", "dims": ["client"],
                 "filters": [{"dim": "month", "op": "=", "value": 7},
                             {"dim": "year", "op": "=", "value": 2026}]}
    nl2sql.call_ollama = fu_model
    nl2sql.call_llm = fu_model
    try:
        fu_prompts.clear()
        r = c.post("/api/explore/intent",
                   json={"question": "а по неделям?", "generator_id": gid_why,
                         "prev_intent": PREV_BODY})
        d = r.json() if r.status_code == 200 else {}
        i = d.get("intent") or {}
        check("follow-up route: a question with no metric word PINS the previous metric",
              r.status_code == 200 and i.get("kpi") == "sl" and d.get("fixed_kpi") == "sl",
              f"{r.status_code} {r.text[:200]}")
        check("follow-up route: the previous period is inherited (the question named none)",
              sorted((f["dim"], f["value"]) for f in i.get("filters") or []) == [("month", 7), ("year", 2026)],
              i.get("filters"))
        check("follow-up route: the new cut is what the model contributed",
              i.get("dims") == ["week"], i.get("dims"))
        p = fu_prompts[-1] if fu_prompts else ""
        check("follow-up route: the prompt carries ONE context line naming the previous answer",
              p.count("Предыдущий вопрос был про метрику") == 1, p[-400:])
        check("follow-up route: that line names the metric, the cut and WHICH time dims",
              "Service level" in p and "client" in p and "month" in p and "year" in p, p[-400:])
        check("follow-up route: and carries NO values — the data boundary holds",
              "2026" not in p.split("Предыдущий вопрос")[-1].split("Вопрос:")[0], p[-400:])
        check("follow-up route: the context line sits BEFORE the question, not after «JSON:»",
              p.index("Предыдущий вопрос") < p.rindex("Вопрос:") < p.rindex("JSON:"), p[-300:])
        check("follow-up route: the metric is pinned, so the model is offered no other",
              "Метрика уже выбрана" in p and "\n- by_hub:" not in p, p[:200])

        # A question that DOES name a metric leaves the model free to choose,
        # and only gets the previous answer named as context.
        fu_prompts.clear()
        r = c.post("/api/explore/intent",
                   json={"question": "какой уровень сервиса по неделям?", "generator_id": gid_why,
                         "prev_intent": PREV_BODY})
        d = r.json() if r.status_code == 200 else {}
        check("follow-up route: a question NAMING a metric is not a pinned follow-up",
              r.status_code == 200 and d.get("fixed_kpi") is None, f"{r.status_code} {r.text[:200]}")

        # Validation of the payload itself — bounded, five keys, a real KPI.
        r = c.post("/api/explore/intent",
                   json={"question": "а по неделям?", "generator_id": gid_why,
                         "prev_intent": {"kpi": "made_up", "dims": [], "filters": []}})
        check("follow-up route: prev_intent naming a KPI the semantics do not have -> 422",
              r.status_code == 422, f"{r.status_code} {r.text[:200]}")
        r = c.post("/api/explore/intent",
                   json={"question": "q", "generator_id": gid_why,
                         "prev_intent": {"kpi": "sl", "sql": "SELECT 1"}})
        check("follow-up route: a key outside kpi/dims/filters/order/limit -> 422",
              r.status_code == 422, f"{r.status_code} {r.text[:200]}")
        # A filter without `op`/`value` used to reach ground_intent and KeyError (500).
        for bad in ({"dim": "year"}, {"dim": "year", "op": "=="}, {"dim": "year", "op": "="}):
            r = c.post("/api/explore/intent",
                       json={"question": "сколько всего?", "generator_id": gid_why,
                             "prev_intent": {"kpi": "sl", "dims": [], "filters": [bad]}})
            check(f"follow-up route: malformed filter {bad} -> 422, never 500",
                  r.status_code == 422, f"{r.status_code} {r.text[:160]}")
        r = c.post("/api/explore/intent",
                   json={"question": "q", "generator_id": gid_why,
                         "prev_intent": {"kpi": "sl", "dims": [str(k) for k in range(20)]}})
        check("follow-up route: an unbounded dims list -> 422", r.status_code == 422, r.status_code)
        r = c.post("/api/explore/intent",
                   json={"question": "q", "generator_id": gid_why,
                         "prev_intent": {"kpi": "sl", "filters": [{"nope": 1}]}})
        check("follow-up route: a malformed filter -> 422", r.status_code == 422, r.status_code)
        r = c.post("/api/explore/intent",
                   json={"question": "q", "generator_id": gid_why, "prev_intent": {}})
        check("follow-up route: prev_intent without a kpi -> 422", r.status_code == 422, r.status_code)

        # prepare/validate: the browser half must ground the same way, or the
        # inherited period would exist only on the server path.
        r = c.post("/api/explore/intent/prepare",
                   json={"question": "а по неделям?", "generator_id": gid_why,
                         "prev_intent": PREV_BODY})
        d = r.json() if r.status_code == 200 else {}
        check("follow-up prepare: pins the previous metric and returns the context prompt",
              r.status_code == 200 and d.get("fixed_kpi") == "sl"
              and "Предыдущий вопрос был про метрику" in (d.get("prompt") or ""),
              f"{r.status_code} {r.text[:200]}")
        check("follow-up prepare: still no cell values in the payload",
              "SECRET-RAW-VALUE" not in r.text)
        r = c.post("/api/explore/intent/validate",
                   json={"question": "а по неделям?", "generator_id": gid_why, "fixed_kpi": "sl",
                         "prev_intent": PREV_BODY,
                         "raw": '{"kpi": "sl", "dims": ["week"], "filters": [], "limit": 10}'})
        d = r.json() if r.status_code == 200 else {}
        i = d.get("intent") or {}
        check("follow-up validate: the browser path inherits the same period",
              r.status_code == 200 and sorted((f["dim"], f["value"]) for f in i.get("filters") or [])
              == [("month", 7), ("year", 2026)], f"{r.status_code} {r.text[:200]}")
        r = c.post("/api/explore/intent/validate",
                   json={"question": "q", "generator_id": gid_why, "raw": '{"kpi": "sl"}',
                         "prev_intent": {"kpi": "made_up"}})
        check("follow-up validate: an unknown prev kpi is a 422 there too", r.status_code == 422,
              r.status_code)
    finally:
        nl2sql.call_ollama = orig
        nl2sql.call_llm = orig_llm

    # ── "why …?" over the wire ─────────────────────────────────────────
    # The real question that produced nonsense SQL: it must come back as a
    # why-answer (a card the page assembles), never as a KPI intent with an
    # ORDER BY/LIMIT on a one-row aggregate.
    why_prompts = []

    def why_model(prompt, fmt=None, max_tokens=None):
        why_prompts.append(prompt)
        return ('{"kpi": "sl", "dims": ["client"], "filters": [], '
                '"order": {"by": "value", "dir": "desc"}, "limit": 1, "explanation": "SL"}')
    nl2sql.call_ollama = why_model
    nl2sql.call_llm = why_model
    try:
        r = c.post("/api/explore/intent",
                   json={"question": "почему в январе 2026 упал SL?", "generator_id": gid_why})
        d = r.json() if r.status_code == 200 else {}
        i = d.get("intent") or {}
        check("why route: a 'why' question is answered as source=why",
              r.status_code == 200 and d.get("source") == "why", f"{r.status_code} {r.text[:200]}")
        check("why route: the answer names the metric it is explaining",
              (d.get("why") or {}).get("kpi") == "sl" and i.get("kpi") == "sl", d.get("why"))
        check("why route: the period is grounded from the question (year 2026 + month 1)",
              sorted((f["dim"], f["value"]) for f in i.get("filters") or []) == [("month", 1), ("year", 2026)],
              i.get("filters"))
        check("why route: `why.period` is the same period, for the page's shiftPeriod",
              (d.get("why") or {}).get("period") == i.get("filters"), d.get("why"))
        check("why route: NO order and NO limit — the defect this feature exists for",
              "order" not in i and "limit" not in i, i)
        check("why route: no grouping either — the card picks its own cuts",
              i.get("dims") == [], i.get("dims"))
        check("why route: the model never saw the word 'why' (pochemu)",
              why_prompts and all("почему" not in p.lower() for p in why_prompts),
              (why_prompts[-1][:200] if why_prompts else "no prompt"))
        check("why route: the model DID see the rest of the question",
              why_prompts and "январе 2026" in why_prompts[-1], (why_prompts[-1][-200:] if why_prompts else ""))

        # A why-question whose remainder quotes a KPI's own question_form is
        # settled deterministically — the model must not be called at all.
        why_prompts.clear()
        nl2sql.call_ollama = never
        nl2sql.call_llm = never
        r = c.post("/api/explore/intent",
                   json={"question": "почему упал уровень сервиса в январе 2026?", "generator_id": gid_why})
        d = r.json() if r.status_code == 200 else {}
        check("why route: a form-matched why-question is answered WITHOUT the model",
              r.status_code == 200 and d.get("source") == "why" and d.get("model") is None
              and (d.get("intent") or {}).get("kpi") == "sl", f"{r.status_code} {r.text[:200]}")

        # prepare: same split as everywhere else — a form match answers here,
        # otherwise the page gets a prompt that is still free of "why".
        r = c.post("/api/explore/intent/prepare",
                   json={"question": "почему упал уровень сервиса в январе 2026?", "generator_id": gid_why})
        d = r.json() if r.status_code == 200 else {}
        check("why prepare: a form-matched why-question comes back as a why ANSWER, no prompt",
              r.status_code == 200 and d.get("source") == "why" and "prompt" not in d
              and (d.get("why") or {}).get("kpi") == "sl", f"{r.status_code} {r.text[:200]}")
        r = c.post("/api/explore/intent/prepare",
                   json={"question": "почему в январе 2026 упал SL?", "generator_id": gid_why})
        d = r.json() if r.status_code == 200 else {}
        check("why prepare: the model path returns a prompt and a schema, still source=why",
              r.status_code == 200 and d.get("source") == "why" and "Метрики:" in (d.get("prompt") or "")
              and (d.get("schema") or {}).get("properties", {}).get("kpi", {}).get("enum") == ["sl", None],
              f"{r.status_code} {r.text[:200]}")
        check("why prepare: the prompt handed to the page carries no why-word",
              "почему" not in (d.get("prompt") or "").lower(), (d.get("prompt") or "")[:200])
        check("why prepare: and still no cell values", "SECRET-RAW-VALUE" not in r.text)

        # validate: the class is re-derived from the question, so a browser
        # that answers a why-question with a full KPI form still gets a card.
        r = c.post("/api/explore/intent/validate",
                   json={"question": "почему в январе 2026 упал SL?", "generator_id": gid_why,
                         "raw": '{"kpi": "sl", "dims": ["client"], "filters": [], '
                                '"order": {"by": "value", "dir": "desc"}, "limit": 1}'})
        d = r.json() if r.status_code == 200 else {}
        i = d.get("intent") or {}
        check("why validate: source=why and the model's dims/order/limit are discarded",
              r.status_code == 200 and d.get("source") == "why" and i.get("dims") == []
              and "order" not in i and "limit" not in i, f"{r.status_code} {r.text[:200]}")
        check("why validate: the period is grounded from the question",
              sorted((f["dim"], f["value"]) for f in i.get("filters") or []) == [("month", 1), ("year", 2026)],
              i.get("filters"))

        # A KPI question over the SAME generator is untouched by all of this.
        nl2sql.call_ollama = why_model
        nl2sql.call_llm = why_model
        r = c.post("/api/explore/intent",
                   json={"question": "сколько всего строк осталось?", "generator_id": gid_why})
        check("why: a question that is not about reasons still routes to the KPI path",
              r.status_code == 200 and r.json().get("source") == "model", f"{r.status_code} {r.text[:160]}")
    finally:
        nl2sql.call_ollama = orig
        nl2sql.call_llm = orig_llm

    # ── tier 2: free SQL over the report's semantic views ─────────────────
    T2_BODY = {"question": "how many rows have no customer?", "generator_id": gid,
               "bindings_status": {"tables": {"orders": "bound", "secret_map": "inline"},
                                   "views": {"lines": "ok"}}}
    nl2sql.call_ollama = never
    nl2sql.call_llm = never
    try:
        r = c.post("/api/explore/semantic-sql/prepare", json=T2_BODY)
        d = r.json() if r.status_code == 200 else {}
        check("tier2 prepare: returns a prompt describing the report's views, no model called",
              r.status_code == 200 and '"lines"' in (d.get("prompt") or "")
              and '"fact_clipped"' in (d.get("prompt") or ""), f"{r.status_code} {r.text[:200]}")
        check("tier2 prepare: reports which relations were offered",
              d.get("relations") == ["lines", "orders", "secret_map"], d.get("relations"))
        check("tier2 prepare: NO cell values in the payload — the boundary that makes this safe",
              "SECRET-RAW-VALUE" not in r.text, r.text[:300])
        check("tier2 prepare: and no KPI SQL", "SUM(fact_clipped)" not in r.text)
        check("tier2 prepare: no JSON schema — the answer is SQL text, not a form",
              d.get("schema") is None, d.get("schema"))
        r = c.post("/api/explore/semantic-sql/prepare", json=dict(T2_BODY, generator_id=99999))
        check("tier2 prepare: unreachable generator -> 404", r.status_code == 404, r.status_code)

        good_sql = 'SELECT "client", COUNT(*) AS n FROM "lines" GROUP BY 1 LIMIT 100'
        r = c.post("/api/explore/semantic-sql/validate",
                   json={"question": "q", "generator_id": gid, "raw": "```sql\n" + good_sql + ";\n```"})
        check("tier2 validate: a fenced SELECT comes back stripped and clean",
              r.status_code == 200 and r.json()["sql"] == good_sql, f"{r.status_code} {r.text[:200]}")
        for bad, what in (
                ("DROP TABLE lines", "a DDL statement"),
                ('SELECT 1; DELETE FROM "lines"', "a second statement behind a semicolon"),
                ('WITH x AS (SELECT 1) INSERT INTO "lines" SELECT * FROM x', "DML behind a CTE"),
                ("SELECT * FROM read_csv('/etc/passwd')", "a file read"),
                ('SELECT "read_csv"(\'/etc/passwd\')', "a QUOTED file read (the guard\'s own trap)"),
                ("this is not sql", "prose"),
        ):
            r = c.post("/api/explore/semantic-sql/validate",
                       json={"question": "q", "generator_id": gid, "raw": bad})
            check(f"tier2 validate: rejects {what} -> 422", r.status_code == 422,
                  f"{r.status_code} {r.text[:160]}")
        r = c.post("/api/explore/semantic-sql/validate",
                   json={"question": "q", "generator_id": 99999, "raw": good_sql})
        check("tier2 validate: unreachable generator -> 404", r.status_code == 404, r.status_code)
    finally:
        nl2sql.call_ollama = orig
        nl2sql.call_llm = orig_llm

    # The server path calls the model itself and runs the answer through the
    # SAME two gates — `_validate_semantic_sql` is shared, so it cannot drift.
    t2_prompts = []

    def t2_model(prompt, fmt=None, max_tokens=None):
        t2_prompts.append((prompt, fmt, max_tokens))
        return "```sql\nSELECT \"client\" FROM \"lines\" LIMIT 10\n```"
    nl2sql.call_llm = t2_model
    try:
        r = c.post("/api/explore/semantic-sql", json=T2_BODY)
        check("tier2 server path: 200 with the stripped SELECT",
              r.status_code == 200 and r.json()["sql"] == 'SELECT "client" FROM "lines" LIMIT 10',
              f"{r.status_code} {r.text[:200]}")
        check("tier2 server path: the prompt is the semantic schema and carries no values",
              t2_prompts and '"lines"' in t2_prompts[-1][0] and "SECRET-RAW-VALUE" not in t2_prompts[-1][0],
              (t2_prompts[-1][0][:200] if t2_prompts else ""))
        check("tier2 server path: no JSON mode (it would corrupt SQL)",
              t2_prompts and t2_prompts[-1][1] is None, t2_prompts[-1][1] if t2_prompts else "")
        check("tier2 server path: SQL gets the bigger token budget at the call site",
              t2_prompts and t2_prompts[-1][2] == nl2sql.MAX_SQL_TOKENS,
              t2_prompts[-1][2] if t2_prompts else "")
        # A truncated JSON form fails loudly; a truncated SELECT can parse fine
        # and answer the wrong question — so SQL gets the bigger budget.
        ds_calls = []
        from app.config import settings as _st
        _st_prov, _st_key = _st.llm_provider, _st.llm_api_key
        _st.llm_provider, _st.llm_api_key = "deepseek", "sk-x"
        _httpx.post = lambda url, **kw: (ds_calls.append(kw.get("json") or {}), _DSResp(200, "SELECT 1"))[1]
        try:
            orig_llm("p", None, nl2sql.MAX_SQL_TOKENS)
            check("tier2: SQL asks for a bigger max_tokens than the intent form",
                  ds_calls[-1]["max_tokens"] == nl2sql.MAX_SQL_TOKENS > nl2sql.MAX_DEEPSEEK_TOKENS,
                  ds_calls[-1].get("max_tokens"))
            orig_llm("p")
            check("tier2: the form path's budget is unchanged",
                  ds_calls[-1]["max_tokens"] == nl2sql.MAX_DEEPSEEK_TOKENS, ds_calls[-1].get("max_tokens"))
        finally:
            _httpx.post = _orig_post
            _st.llm_provider, _st.llm_api_key = _st_prov, _st_key
        nl2sql.call_llm = lambda p, fmt=None, max_tokens=None: "UPDATE lines SET client = 'x'"
        r = c.post("/api/explore/semantic-sql", json=T2_BODY)
        check("tier2 server path: a non-SELECT answer is 422, never handed to the page",
              r.status_code == 422, f"{r.status_code} {r.text[:160]}")
    finally:
        nl2sql.call_llm = orig_llm

    # llm-config's `server` flag: the page decides one-shot-with-a-cut vs
    # Grand-Total-plus-form on it, so it must be honest about this process.
    from app.config import settings as _st0
    r = c.get("/api/explore/llm-config")
    check("llm-config: `server` is true while OP_LLM_ENDPOINT points somewhere (ollama)",
          r.json().get("server") is True, r.text[:200])
    _saved_ep = _st0.llm_endpoint
    _st0.llm_endpoint = ""
    try:
        r = c.get("/api/explore/llm-config")
        check("llm-config: no endpoint -> `server` false (the page runs the Grand Total instead)",
              r.json().get("server") is False, r.text[:200])
    finally:
        _st0.llm_endpoint = _saved_ep

    # ── llm-config: what the page may do, and the key gating ─────────────
    from app.config import settings as _st
    r = c.get("/api/explore/llm-config")
    d = r.json()
    check("llm-config: browser off by default and no key handed out",
          r.status_code == 200 and d["browser"] is False and d["key"] is None, r.text[:200])
    _st.llm_provider, _st.llm_browser = "deepseek", True
    _st.llm_endpoint = ""     # prod shape: browser path, no server provider at all
    # DeepSeek is per-user allowlisted (2026-08-23): grant every fixture user
    # so the checks below keep testing THEIR rule (generator reach / BYOK),
    # not the allowlist. The allowlist's own smoke is tests/smoke_llm_config.py.
    from app.database import SessionLocal as _SL
    from app.models import LlmAccess as _LA, User as _U

    def _grant_all_llm():
        _dbx = _SL()
        for _u in _dbx.query(_U).all():
            if not _dbx.query(_LA).filter(_LA.user_id == _u.id).first():
                _dbx.add(_LA(user_id=_u.id))
        _dbx.commit()
        _dbx.close()
    _grant_all_llm()
    try:
        r = c.get("/api/explore/llm-config")
        d = r.json()
        check("llm-config: the browser path needs no server endpoint or server key to be usable",
              d["provider"] == "deepseek" and d["endpoint"] == "https://api.deepseek.com"
              and d["model"] == "deepseek-chat" and d["browser"] is True, r.text[:200])
        check("llm-config: no OP_LLM_BROWSER_KEY -> key is null (BYOK)", d["key"] is None, r.text[:200])
        _st.llm_browser_key = "sk-server-held"
        r = c.get("/api/explore/llm-config")
        check("llm-config: a user who may run a generator gets the server key",
              r.json()["key"] == "sk-server-held", r.text[:200])
    finally:
        pass

    # Reach is the generator's: a user who cannot run any dashboard of this
    # generator gets 404 — same answer as "does not exist", no probing.
    r = c.post("/api/admin/users", json={"person_id": "olga", "role": "visitor"})
    assert r.status_code in (200, 201), r.text[:200]
    r = c.post("/dev-login", data={"person_id": "olga"}, follow_redirects=False)
    assert r.status_code == 302
    _grant_all_llm()  # olga too — her check is about generator reach, not the allowlist
    nl2sql.call_ollama = fake
    try:
        r = c.post("/api/explore/intent", json={"question": "q", "generator_id": gid})
        check("intent route: a real generator outside the user's reach -> 404", r.status_code == 404, f"{r.status_code} {r.text[:160]}")
        r = c.post("/api/explore/intent/prepare", json={"question": "q", "generator_id": gid})
        check("prepare: a real generator outside the user's reach -> 404", r.status_code == 404, r.status_code)
        r = c.get("/api/explore/llm-config")
        check("llm-config: a user who may run NO generator never gets the server key",
              r.status_code == 200 and r.json()["key"] is None and r.json()["browser"] is True, r.text[:200])
        r = c.get("/api/explore/catalog")
        check("explore snapshot for that user carries no semantics of the unreachable generator",
              r.status_code == 200 and not any(s.get("generator") == "intent-test" for s in (r.json().get("semantics") or [])),
              r.text[:200])
    finally:
        nl2sql.call_ollama = orig


# ── Stand 2026-09-06: "in week 23 of 2026" parsed as weeks {20, 23} (the year's
# first digits after the word "week"), and a named week was dropped for a KPI without
# a week cut — the verifier then asked for the week the question had named.
(lambda: (
    check("times: 'in week 23 of 2026' names week 23 only, year 2026",
          nl2sql._question_times("какой уровень недопоставки на 23 неделе 2026?") == {"year": {2026}, "month": set(), "week": {23}},
          str(nl2sql._question_times("какой уровень недопоставки на 23 неделе 2026?"))),
    check("times: 'week 2026' names no week",
          nl2sql._question_times("неделя 2026")["week"] == set()),
    check("ground: a NAMED week survives for a KPI without a week cut (engine drops it with a note)",
          any(f.get("dim") == "week" and f.get("value") == 23 for f in
              nl2sql.ground_intent("какой уровень недопоставки на 23 неделе 2026?",
                                   {"kpi": "k", "dims": [], "filters": [{"dim": "year", "op": "=", "value": 2026}], "_allowed_dims": ["year", "month"]}).get("filters") or [])),
))()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES: {FAILURES}")
    sys.exit(1)
print("ALL PASS")
