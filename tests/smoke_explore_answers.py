"""Smoke test: serverside AI-answer record (`explore_answers`, no data).

Pins:
  * a row is written on every recorded success point — `/api/explore/intent`
    (form AND model source), `/api/explore/intent/validate`,
    `/api/explore/semantic-sql/validate`, and `lake/exec` ops `intent`/`sql`;
  * the SQL text itself is never stored anywhere in the row — only its
    sha256 — even though the generated/validated SQL text is known to the
    test (it is asserted absent from every column, not just missing a `sql`
    column);
  * `GET /api/explore/answers` returns only the caller's own rows for an
    ordinary user, and every row for an administrator (`view_admin`);
  * the hot path (`GET /d/{id}`) still renders after `explore_answers` rows
    exist — this feature adds no read on that path.

    .venv/bin/python tests/smoke_explore_answers.py
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-smoke-answers-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_EXPLORE_ENABLED": "true",
    "OP_SAML_SP_BASE_URL": "http://testserver",
    "OP_LLM_ENDPOINT": "http://127.0.0.1:1",  # never reached: call_ollama is stubbed
})
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests", "chrome"))
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import nl2sql  # noqa: E402
import seed_explore  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def build_part(path, cols, rows, partition, fname):
    import duckdb
    con = duckdb.connect(":memory:")
    selects = []
    for r in rows:
        cells = ", ".join(
            ["'" + str(v).replace("'", "''") + "' AS \"" + c + "\"" for c, v in zip(cols, r)]
            + [f"'{partition}' AS __partition", "'" + fname + "' AS __file"])
        selects.append("SELECT " + cells)
    con.execute("COPY (" + " UNION ALL ".join(selects) + ") TO '" + path + "' (FORMAT PARQUET)")
    con.close()


SEM = {
    "schema_version": 1, "generator": "answers-test",
    "glossary": {"SL": "Share of the ordered quantity delivered"},
    "rules": {"reporting_start": "2026-06-01"},
    "tables": {
        "orders": {"description": "Orders", "sources": [{"file": "SL_*.xlsx", "sheet": "Orders"}],
                   "columns": {"client": {"source": "Customer", "type": "text", "role": "dim"},
                               "week": {"source": "Week", "type": "text", "role": "time"},
                               "order_qty": {"source": "Order, pcs", "type": "number", "role": "measure"},
                               "fact_qty": {"source": "Actual", "type": "number", "role": "measure"}}},
    },
    "views": {"lines": "SELECT o.*, least(order_qty, fact_qty) AS fact_clipped FROM orders o"},
    "kpis": {"sl": {"name": "Service level", "definition": "Σmin(actual,order)/Σorder", "aggregation": "ratio",
                    "question_forms": ["what is the SL", "service level"],
                    "cell_sql": "SELECT client, week, SUM(fact_clipped) AS n, SUM(order_qty) AS d "
                                "FROM lines GROUP BY 1, 2"}},
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


with TestClient(app) as c:
    from app.database import SessionLocal
    from app.models import ExploreAnswer, GeneratorVersion, User

    # ── fixture: one generator+dashboard for the intent/semantic-sql tiers ──
    login(c, "admin1")
    r = c.post("/api/admin/generators", files={"bundle": ("b.zip", bundle("answers-test", SEM), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Answers", "generator_id": gid})
    assert r.status_code == 201, r.text[:300]
    answers_dash_id = r.json()["id"]

    db = SessionLocal()
    from app.models import Dashboard as _Dash
    db.query(_Dash).filter(_Dash.generator_id == gid).update({"is_public": True})
    db.commit()
    admin = db.query(User).filter(User.person_id == "admin1").first()
    admin_id = admin.id
    ver = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == gid, GeneratorVersion.is_current == True).first())  # noqa: E712
    exp_version, exp_sem_sha = ver.version_number, sha(ver.semantics_json)
    db.close()

    def rows_for(user_id=None, tier=None):
        d = SessionLocal()
        q = d.query(ExploreAnswer)
        if user_id is not None:
            q = q.filter(ExploreAnswer.user_id == user_id)
        if tier is not None:
            q = q.filter(ExploreAnswer.tier == tier)
        out = list(q.all())
        d.close()
        return out

    def dump_all_text():
        """Every column of every row, concatenated — the surface a stray SQL
        string would have to hide on if it leaked anywhere in the table."""
        d = SessionLocal()
        out = []
        for a in d.query(ExploreAnswer).all():
            out.append(json.dumps({
                "question": a.question, "tier": a.tier, "intent_json": a.intent_json,
                "sql_sha256": a.sql_sha256, "via": a.via,
            }, ensure_ascii=False))
        d.close()
        return "\n".join(out)

    before = len(rows_for())

    # ── /api/explore/intent — form source (no model) ────────────────────
    r = c.post("/api/explore/intent", json={"question": "what is the SL by chain?", "generator_id": gid})
    check("intent (form): 200", r.status_code == 200, r.text[:200])
    check("intent (form): the response carries the id of the row it wrote",
          isinstance(r.json().get("answer_id"), int), r.text[:200])
    form_answer_id = r.json().get("answer_id")
    new = rows_for(user_id=admin_id, tier="form")
    check("intent (form): one explore_answers row written", len(new) == 1, len(new))
    if new:
        a = new[0]
        check("intent (form): via=server", a.via == "server", a.via)
        check("intent (form): generator_id/version/semantics pinned",
              a.generator_id == gid and a.generator_version == exp_version
              and a.semantics_sha256 == exp_sem_sha,
              (a.generator_id, a.generator_version, a.semantics_sha256))
        check("intent (form): intent_json carries the KPI", (a.get_intent() or {}).get("kpi") == "sl", a.intent_json)
        check("intent (form): dashboard_id is null (generator-scoped route)", a.dashboard_id is None)
        check("intent (form): question stored as-is", a.question == "what is the SL by chain?", a.question)
        check("intent (form): answer_id in the body IS that row", form_answer_id == a.id,
              (form_answer_id, a.id))
        check("intent (form): a fresh answer carries no vote", a.vote is None and a.voted_at is None,
              (a.vote, a.voted_at))

    # ── /api/explore/intent — model source ───────────────────────────────
    orig_ollama = nl2sql.call_ollama

    def fake(prompt, fmt=None):
        return '{"kpi": "sl", "dims": ["client"], "filters": [], "limit": 10, "explanation": "by chain"}'
    nl2sql.call_ollama = fake
    try:
        r = c.post("/api/explore/intent", json={"question": "how many rows are left in total?", "generator_id": gid})
        check("intent (model): 200", r.status_code == 200, r.text[:200])
        check("intent (model): answer_id returned", isinstance(r.json().get("answer_id"), int), r.text[:200])
    finally:
        nl2sql.call_ollama = orig_ollama
    new = rows_for(user_id=admin_id, tier="intent")
    check("intent (model): one 'intent'-tier row written", len(new) == 1, len(new))

    # ── /api/explore/intent/validate — browser path ──────────────────────
    raw = '{"kpi": "sl", "dims": ["client"], "filters": [], "limit": 5, "explanation": "SL"}'
    r = c.post("/api/explore/intent/validate", json={"question": "what is the SL?", "generator_id": gid, "raw": raw})
    check("intent/validate: 200", r.status_code == 200, r.text[:200])
    check("intent/validate: answer_id returned", isinstance(r.json().get("answer_id"), int), r.text[:200])
    new = [a for a in rows_for(user_id=admin_id, tier="intent") if a.via == "browser"]
    check("intent/validate: one browser-via row written", len(new) == 1, len(new))
    if new:
        check("intent/validate: intent_json round-trips", (new[0].get_intent() or {}).get("dims") == ["client"])

    # ── /api/explore/semantic-sql/validate ───────────────────────────────
    good_sql = 'SELECT "client", COUNT(*) AS n FROM "lines" GROUP BY 1 LIMIT 100'
    r = c.post("/api/explore/semantic-sql/validate",
               json={"question": "how many rows?", "generator_id": gid, "raw": "```sql\n" + good_sql + ";\n```"})
    check("semantic-sql/validate: 200", r.status_code == 200 and r.json()["sql"] == good_sql, r.text[:200])
    check("semantic-sql/validate: answer_id returned",
          isinstance(r.json().get("answer_id"), int), r.text[:200])
    ssql_rows = rows_for(user_id=admin_id, tier="semantic_sql")
    check("semantic-sql/validate: one row written", len(ssql_rows) == 1, len(ssql_rows))
    if ssql_rows:
        a = ssql_rows[0]
        check("semantic-sql/validate: via=browser", a.via == "browser", a.via)
        check("semantic-sql/validate: sql_sha256 matches the validated SQL",
              a.sql_sha256 == sha(good_sql), (a.sql_sha256, sha(good_sql)))
        check("semantic-sql/validate: intent_json is empty (this tier has no intent)", a.intent_json is None)

    # A rejected (422) semantic-sql/validate must NOT write a row.
    before_bad = len(rows_for(tier="semantic_sql"))
    r = c.post("/api/explore/semantic-sql/validate",
               json={"question": "q", "generator_id": gid, "raw": "DROP TABLE lines"})
    check("semantic-sql/validate: rejected SQL -> 422", r.status_code == 422, r.status_code)
    check("semantic-sql/validate: rejected SQL writes nothing",
          len(rows_for(tier="semantic_sql")) == before_bad)

    # ── no SQL text anywhere in the table, only the hash ─────────────────
    dump = dump_all_text()
    check("no row stores the raw validated SQL text",
          good_sql not in dump and "DROP TABLE" not in dump, dump[:300])
    check("no row stores the KPI's cell_sql", "fact_clipped" not in dump)

    # ── lake exec: op 'intent' and op 'sql' ──────────────────────────────
    login(c, "admin1")
    seeded = seed_explore.seed(c)
    dash_id = seeded["dashboard"]
    lake_gid = seeded["generator"]
    snap = c.get("/api/explore/catalog").json()
    by_name = {t["name"]: t for t in snap["tables"]}
    sem_entry = next(e for e in snap["semantics"] if e["generator_id"] == lake_gid)
    EXEC = f"/api/dashboards/{dash_id}/lake/exec"

    ORDERS = {"DataSalesOrder_07.26.xlsx": [("O1", "01.07.2026", "ClientA", "S1", "100"),
                                            ("O2", "02.07.2026", "ClientA", "S2", "200")]}
    REALS = {"DataRealisation_07.26.xlsx": [("O1", "04.07.2026", "S1", "100")]}
    plan = [
        ("orders", seed_explore.ORDER_COLS, ORDERS),
        ("realisations", seed_explore.REAL_COLS, REALS),
    ]
    files = []
    key_n = 0
    for sem_table, cols, data in plan:
        m = sem_entry["bindings"][sem_table]["matches"][0]
        ct = by_name[m["table"]]
        for fname, rows in data.items():
            part = next(p for p in ct["partitions"] if p["file"] == fname)
            key_n += 1
            key = f"{key_n:016x}"
            path = os.path.join(TMP, key + ".parquet")
            build_part(path, cols, rows, part["key"], fname)
            r = c.post(f"/api/dashboards/{dash_id}/lake/parts",
                       files={"file": (key + ".parquet", open(path, "rb").read(), "application/octet-stream")},
                       data={"key": key})
            assert r.status_code in (200, 201), (r.status_code, r.text[:300])
            files.append({"name": fname, "size": 5000, "mtime_ms": part["mtime_ms"] or 1785000000000,
                          "sheet": part.get("sheet"), "header_row": 2, "key": key,
                          "table": m["table"], "partition": part["key"], "bytes": os.path.getsize(path)})

    r = c.put(f"/api/dashboards/{dash_id}/lake/manifest",
              json={"run_id": 42, "parser_version": "4", "files": files})
    check("lake manifest published", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

    before_lake = len(rows_for(tier="lake"))
    # The `intent` op only builds SQL text (it never executes, and the page
    # never marks it `final`) — no row, matching the real widget's flow
    # (static/js/explore.js `send('explore:intent', …)` never sets `final`).
    r = c.post(EXEC, json={"op": "intent", "question": "order amount by warehouse?",
                          "intent": {"generator_id": lake_gid, "kpi": "order_amount", "dims": []}})
    check("lake exec intent: 200, no error", r.status_code == 200 and r.json().get("error") is None, r.text[:300])
    gen_sql = r.json().get("sql")
    check("lake exec intent: writes no answer row (not the final call)",
          len(rows_for(tier="lake")) == before_lake, len(rows_for(tier="lake")))

    # A plain (non-final) `sql` op — a why-card sub-query, say — also writes
    # nothing: this is the fix for "2-3 rows per answer" (intent+sql pair,
    # or several whyRun sub-queries all sharing one sql_sha256/answer).
    r = c.post(EXEC, json={"op": "sql", "sql": gen_sql, "limit": 100})
    check("lake exec sql (non-final): 200", r.status_code == 200, r.text[:300])
    check("lake exec sql (non-final): no answer_id — there is no answer row to vote on",
          r.json().get("answer_id") is None, str(r.json())[:200])
    check("lake exec sql (non-final): writes no answer row",
          len(rows_for(tier="lake")) == before_lake, len(rows_for(tier="lake")))

    # The ONE `sql` op the widget marks `final` (with the question it
    # answers) is what writes the row — exactly once per user-visible answer.
    r = c.post(EXEC, json={"op": "sql", "sql": gen_sql, "limit": 100,
                          "final": True, "question": "order amount by warehouse?"})
    check("lake exec sql (final): 200", r.status_code == 200, r.text[:300])
    check("lake exec sql (final): the result carries the answer_id 👍/👎 needs",
          isinstance(r.json().get("answer_id"), int), str(r.json())[:200])
    lake_answer_id = r.json().get("answer_id")
    lake_rows = rows_for(tier="lake")
    check("lake exec sql (final): exactly one answer row written",
          len(lake_rows) == before_lake + 1, len(lake_rows))
    if len(lake_rows) == before_lake + 1:
        a = lake_rows[-1]
        check("lake exec sql (final): via=server, dashboard_id set, run_id=manifest run_id",
              a.via == "server" and a.dashboard_id == dash_id and a.run_id == 42,
              (a.via, a.dashboard_id, a.run_id))
        check("lake exec sql (final): sql_sha256 matches the SQL that ran",
              a.sql_sha256 == sha(gen_sql), (a.sql_sha256, gen_sql))
        check("lake exec sql (final): intent_json empty (lake tier never stores it)",
              a.get_intent() is None, a.intent_json)
        check("lake exec sql (final): question carried through from the request body",
              a.question == "order amount by warehouse?", a.question)
        check("lake exec sql (final): answer_id in the body IS that row",
              lake_answer_id == a.id, (lake_answer_id, a.id))

    # A guard-rejected 'sql' op (engine error), even marked final, must not
    # write a row — only a SUCCESSFUL final answer is recorded.
    before_guard = len(rows_for(tier="lake"))
    r = c.post(EXEC, json={"op": "sql", "sql": "SELECT * FROM read_parquet('/etc/passwd')",
                          "final": True, "question": "q"})
    check("lake exec sql: hostile SQL -> engine error, not a 500",
          r.status_code == 200 and r.json().get("error"), r.text[:200])
    check("lake exec sql: hostile SQL writes no answer row", len(rows_for(tier="lake")) == before_guard)

    # 'status' and 'export' never write an answer row.
    r = c.post(EXEC, json={"op": "status"})
    assert r.status_code == 200
    check("lake exec status: writes no answer row", len(rows_for(tier="lake")) == before_guard)

    dump2 = dump_all_text()
    check("no row stores the lake-generated SQL text either", gen_sql not in dump2, dump2[:300])

    # ── read permissions: GET /api/explore/answers ───────────────────────
    r = c.post("/api/admin/users", json={"person_id": "olga", "role": "superuser"})
    assert r.status_code == 200, r.text[:200]
    olga_id = r.json()["id"]
    from app.models import DashboardPermission
    db = SessionLocal()
    db.add(DashboardPermission(dashboard_id=answers_dash_id, user_id=olga_id, can_update_data=True))
    db.commit()
    db.close()
    login(c, "olga")
    r = c.post("/api/explore/intent", json={"question": "what is the SL by chain?", "generator_id": gid})
    check("olga can also answer (fixture generator is public)", r.status_code == 200, r.text[:200])
    r = c.get("/api/explore/answers")
    check("olga: GET /answers -> 200", r.status_code == 200, r.text[:200])
    ans = r.json().get("answers", [])
    check("olga sees only her own rows, not admin1's",
          len(ans) >= 1 and all(a["user_id"] == olga_id for a in ans), ans)

    login(c, "admin1")
    r = c.get("/api/explore/answers")
    check("admin1: GET /answers -> 200", r.status_code == 200, r.text[:200])
    ans_admin = r.json().get("answers", [])
    check("admin1 (view_admin) sees rows from both users",
          len({a["user_id"] for a in ans_admin}) >= 2, {a["user_id"] for a in ans_admin})

    r = c.get(f"/api/explore/answers?generator={gid}")
    check("filter by generator narrows the list",
          all(a["generator_id"] == gid for a in r.json()["answers"]), r.json())

    # ── hot path unaffected: /d/{id} still renders ───────────────────────
    r = c.get(f"/d/{dash_id}")
    check("hot path: GET /d/{id} still 200 after explore_answers rows exist",
          r.status_code == 200, r.status_code)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES"); sys.exit(1)
print("ALL PASS")
