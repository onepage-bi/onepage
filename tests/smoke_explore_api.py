"""Smoke test for the Explore API (app/api/explore.py, app/services/nl2sql.py).

Fixture: one folder/table built through the REAL API, the same way
tests/smoke_catalog_api.py builds its fixture (bundle -> dashboard -> run ->
observations -> catalog.rebuild), with a column `profiles` entry on the
observation so `quality_results` carries a `check_type='profile'` row this
module's value-gathering can read; a `ValueBaseline` row is inserted
directly (the known_values-check auto-seed path is a separate feature and
not what this file is testing).

What is asserted: catalog snapshot scope (foreign user sees no tables, an
admin sees confirmed+inferred but never rejected), values bounded to
<=500 entries and <=64 chars each; nl2sql 503 with no LLM endpoint, 422 when
the (monkeypatched) model answers something that is not a single SELECT, 200
+ an audit row when it answers a real SELECT; saved questions CRUD scoped
per user with the `shared` read-only visibility; the kill switch 404s every
route.
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-explore-api-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_EXPLORE_ENABLED": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import catalog, nl2sql  # noqa: E402

FAILURES = []
MAIN = "OPS_Explore"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ───────────────────────────── fixture data ────────────────────────────────

COLS = ["Item", "Order", "Date", "Quantity", "Amount", "Warehouse"]
CORPUS = [
    ("DataSalesOrder_06.26.xlsx", "TDSheet", 4000),
    ("DataSalesOrder_07.26.xlsx", "TDSheet", 1200),
]


def make_bundle(name):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "gen.main"},
         "outputs": {"html": True, "slots": []}, "engine": "browser"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


def create_user(c, person_id, role="visitor"):
    r = c.post("/api/admin/users", json={"person_id": person_id, "role": role})
    assert r.status_code in (200, 201, 409), f"create_user {person_id}: {r.status_code} {r.text[:200]}"


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def register(c, gen_name, title):
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle(gen_name), "application/zip")})
    assert r.status_code == 201, f"register {gen_name}: {r.status_code} {r.text[:300]}"
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
    assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
    return gid, r.json()["id"]


def manifest_files():
    return [{"name": r[0], "size": 1000 + i * 7, "mtime_ms": 1700000000000 - i * 1000}
            for i, r in enumerate(CORPUS)]


LONG_VALUE = "x" * 200  # must be trimmed to <=64 chars in the snapshot


def observed_files():
    files = []
    for i, (name, sheet, rows) in enumerate(CORPUS):
        top = [[f"O-{1000 + i}", 5], [f"O-{2000 + i}", 3], [LONG_VALUE, 1]]
        files.append({
            "name": name, "kind": "parsed",
            "schema": {"sheets": [{"name": sheet, "header": list(COLS), "rows": rows, "header_row": 0}]},
            "consumed": {"sheets": {sheet: {"columns": COLS[:2], "all": False}}},
            "profiles": [{"sheet": sheet, "column": "Order", "type": "string",
                         "nulls": 0, "distinct": 9, "top": top, "sampled": False}],
        })
    return files


def run_once(c, dash):
    r = c.post(f"/api/dashboards/{dash}/generator/runs",
               json={"engine": "browser",
                     "input_manifest": {"dirs": [MAIN], "files": manifest_files()}})
    assert r.status_code == 201, f"run start: {r.status_code} {r.text[:300]}"
    run = r.json()["run_id"]
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/observations",
               json={"seq": 1, "final": True, "files": observed_files()})
    assert r.status_code == 200, f"observations: {r.status_code} {r.text[:400]}"
    r = c.post(f"/api/dashboards/{dash}/generator/runs/{run}/error",
               json={"message": "smoke: closing run"})
    assert r.status_code == 200
    return run


def get_user_id(person_id):
    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    u = db.query(User).filter(User.person_id == person_id).first()
    db.close()
    return u.id if u else None


def get_data_source_id(key_name):
    from app.database import SessionLocal
    from app.models import DataSource
    db = SessionLocal()
    s = db.query(DataSource).filter(DataSource.key_name == key_name).first()
    db.close()
    return s.id if s else None


def seed_baseline(ds_id, generator_id, sheet, column, values):
    from app.database import SessionLocal
    from app.models import ValueBaseline
    db = SessionLocal()
    vb = ValueBaseline(data_source_id=ds_id, generator_id=generator_id,
                       match="*", sheet=sheet, column=column)
    vb.set_values(values)
    db.add(vb)
    db.commit()
    db.close()


def make_permission(dash_id, user_id, can_update_data):
    from app.database import SessionLocal
    from app.models import DashboardPermission
    db = SessionLocal()
    db.add(DashboardPermission(dashboard_id=dash_id, user_id=user_id, can_update_data=can_update_data))
    db.commit()
    db.close()


def check_is_select_only():
    """Pure-function pin for `nl2sql.is_select_only`'s hardened gate: a
    CTE prefix must not smuggle DML/DDL/engine-control past the
    `^(select|with)` anchor, and a legitimate string literal that happens
    to spell a forbidden word must not false-positive."""
    check("is_select_only: plain SELECT", nl2sql.is_select_only("SELECT 1"))
    check("is_select_only: WITH...SELECT", nl2sql.is_select_only("WITH t AS (SELECT 1) SELECT * FROM t"))
    check("is_select_only: rejects CTE-prefixed INSERT",
          not nl2sql.is_select_only("WITH t AS (SELECT 1) INSERT INTO x SELECT * FROM t"))
    check("is_select_only: rejects read_csv_auto",
          not nl2sql.is_select_only("SELECT * FROM read_csv_auto('/etc/passwd')"))
    check("is_select_only: rejects a second statement after semicolon",
          not nl2sql.is_select_only("SELECT 1; DROP TABLE t"))
    check("is_select_only: rejects bare ATTACH",
          not nl2sql.is_select_only("ATTACH 'x.db' AS y"))
    check("is_select_only: a string literal spelling a forbidden word is not a false positive",
          nl2sql.is_select_only("SELECT * FROM t WHERE status = 'drop'"))


def main():
    check_is_select_only()

    with TestClient(app) as c:
        login(c, "admin1")
        gid, dash_id = register(c, "gen-explore", "Explore Fixture")
        run_once(c, dash_id)

        from app.database import SessionLocal
        db = SessionLocal()
        stats = catalog.rebuild(db, budget_s=60.0)
        db.close()
        check("fixture rebuild produced a table", stats["tables"] > 0, str(stats))

        r = c.get("/api/catalog/tables")
        body = r.json()
        table_id = body["tables"][0]["table_id"]
        ds_id = get_data_source_id(MAIN)
        check("fixture: data source resolved", ds_id is not None)

        # a baseline value beyond the profile's own top, to prove the two
        # sources union rather than one shadowing the other
        seed_baseline(ds_id, gid, "TDSheet", "Order", ["O-base-1", "O-base-2"])

        # ── admin catalog snapshot: shape + scope ───────────────────────
        r = c.get("/api/explore/catalog")
        check("admin snapshot: 200", r.status_code == 200, str(r.status_code))
        snap = r.json()
        check("snapshot carries the fixture table",
              any(t["table_id"] == table_id for t in snap["tables"]), str(snap["tables"]))
        check("snapshot never carries a rejected table (none in fixture, trivially true)",
              all(t["status"] in ("confirmed", "inferred") for t in snap["tables"]),
              str([t["status"] for t in snap["tables"]]))
        t = next(t for t in snap["tables"] if t["table_id"] == table_id)
        check("table carries source.key_name", t["source"]["key_name"] == MAIN, str(t["source"]))
        check("columns carry name/display/type only",
              all(set(col.keys()) == {"name", "display", "type"} for col in t["columns"]),
              str(t["columns"][:2]))
        check("partitions carry key/file/sheet/rows/mtime_ms/size",
              all({"key", "file", "sheet", "rows", "mtime_ms", "size"} <= set(p) for p in t["partitions"]),
              str(t["partitions"]))
        check("header_row present", "header_row" in t, str(t))

        vkey = f"{table_id}.order"
        check("values key present for the profiled+baselined column",
              vkey in snap["values"], str(sorted(snap["values"].keys()))[:300])
        vals = snap["values"].get(vkey, [])
        check("values union baseline + profile top",
              {"O-base-1", "O-base-2", "O-1000", "O-2000"} <= set(vals), str(vals))
        check("every value trimmed to <=64 chars",
              all(len(v) <= 64 for v in vals), str([len(v) for v in vals if len(v) > 64]))
        check("the long profile value survived, but trimmed",
              any(v == LONG_VALUE[:64] for v in vals), str([v for v in vals if v.startswith("xx")][:1])[:80])
        check("values list bounded to <=500", len(vals) <= 500, str(len(vals)))

        # ── foreign user has no reach: empty snapshot ───────────────────
        create_user(c, "foreign1")
        login(c, "foreign1")
        r = c.get("/api/explore/catalog")
        check("foreign user: snapshot is empty", r.status_code == 200 and r.json()["tables"] == [],
              str(r.json()))

        login(c, "admin1")

        # ── nl2sql: kill switch (empty endpoint) -> 503 ─────────────────
        check("llm_endpoint defaults empty", settings.llm_endpoint == "", settings.llm_endpoint)
        r = c.post("/api/explore/nl2sql", json={"question": "how many orders?", "table_ids": [table_id]})
        check("nl2sql with no LLM endpoint: 503", r.status_code == 503, f"{r.status_code} {r.text[:200]}")

        # ── nl2sql: unknown table_id -> 422 before ever calling the model ─
        r = c.post("/api/explore/nl2sql", json={"question": "x", "table_ids": ["t_doesnotexist"]})
        check("nl2sql with an unreachable table_id: 422", r.status_code == 422, str(r.status_code))

        # ── nl2sql: monkeypatched model, non-SELECT answer -> 422 ───────
        settings.llm_endpoint = "http://ollama.invalid:11434"
        captured_prompt = {}
        orig_call = nl2sql.call_ollama

        def fake_call_bad(prompt, fmt=None):
            captured_prompt["text"] = prompt
            return "```sql\nDROP TABLE foo;\n```"

        nl2sql.call_ollama = fake_call_bad
        try:
            r = c.post("/api/explore/nl2sql",
                      json={"question": "how many orders?", "table_ids": [table_id]})
            check("nl2sql non-SELECT answer: 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
        finally:
            nl2sql.call_ollama = orig_call

        # prompt pin: OP_LLM_EXAMPLE_VALUES defaults to 0 (plan: "the LLM gets
        # only the schema … never values"), so NO real cell value appears
        # in the prompt at all, not merely capped to 3.
        prompt_text = captured_prompt.get("text", "")
        check("llm_example_values defaults to 0", settings.llm_example_values == 0,
              settings.llm_example_values)
        leaked = [v for v in vals if v and v in prompt_text]
        check("prompt carries no example values at all by default",
              leaked == [], str(leaked)[:200])

        # opting in (OP_LLM_EXAMPLE_VALUES=3) puts the first 3 examples in,
        # and never more than 3 even if raised further
        settings.llm_example_values = 3
        captured_prompt.clear()
        nl2sql.call_ollama = fake_call_bad
        try:
            c.post("/api/explore/nl2sql", json={"question": "x", "table_ids": [table_id]})
        finally:
            nl2sql.call_ollama = orig_call
        prompt_text2 = captured_prompt.get("text", "")
        check("opted-in prompt does carry the first example", vals[0] in prompt_text2 if vals else True,
              prompt_text2[:200])
        leaked2 = [v for v in vals[3:] if v and v in prompt_text2]
        check("opted-in prompt still caps at 3 examples per column",
              leaked2 == [], str(leaked2)[:200])
        settings.llm_example_values = 0

        # ── nl2sql: monkeypatched model, real SELECT -> 200 + audit row ──
        def fake_call_good(prompt, fmt=None):
            return "SELECT COUNT(*) AS n FROM t;\n"

        nl2sql.call_ollama = fake_call_good
        try:
            r = c.post("/api/explore/nl2sql",
                      json={"question": "how many orders?", "table_ids": [table_id]})
            check("nl2sql SELECT answer: 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            out = r.json()
            check("nl2sql returns sql/explanation/model",
                  set(out.keys()) == {"sql", "explanation", "model"}, str(out))
            check("nl2sql sql is stripped of the fence/semicolon-safe SELECT",
                  out["sql"].strip().upper().startswith("SELECT"), str(out["sql"]))
        finally:
            nl2sql.call_ollama = orig_call
            settings.llm_endpoint = ""

        from app.models import AuditLog
        db = SessionLocal()
        audit_rows = db.query(AuditLog).filter(AuditLog.action == "explore_nl2sql").all()
        db.close()
        check("nl2sql call was audited", len(audit_rows) >= 1, str(len(audit_rows)))

        # ── client-side failure log (2026-08-28) ─────────────────────────
        r = c.post("/api/explore/client-error",
                   json={"stage": "provider_http", "reason": "api.deepseek.com answered 429. rate limit",
                         "question": "what is the SL at Delta?", "generator_id": 7, "status": 429,
                         "host": "api.deepseek.com", "tier": 1})
        check("client-error: accepted", r.status_code == 200 and r.json().get("ok") is True,
              f"{r.status_code} {r.text[:200]}")
        db = SessionLocal()
        rows = db.query(AuditLog).filter(AuditLog.action == "explore_client_error").all()
        db.close()
        check("client-error: one audit row, resource explore/<generator_id>",
              len(rows) == 1 and rows[0].resource_type == "explore" and rows[0].resource_id == "7",
              str([(x.resource_type, x.resource_id) for x in rows]))
        d = rows[0].get_detail() if rows else {}
        check("client-error: the row carries stage/status/host/question and via=browser",
              d.get("stage") == "provider_http" and d.get("status") == 429
              and d.get("host") == "api.deepseek.com" and "Delta" in d.get("question", "")
              and d.get("via") == "browser", str(d))
        r = c.post("/api/explore/client-error", json={"stage": "DROP TABLE", "reason": "x"})
        check("client-error: a stage outside [a-z0-9_] is 422", r.status_code == 422, str(r.status_code))
        r = c.post("/api/explore/client-error", json={"stage": "query_failed", "reason": "x" * 600})
        check("client-error: an oversized reason is 422, not truncated silently",
              r.status_code == 422, str(r.status_code))

        # ── saved questions: server-side gates on save ───────────────────
        r = c.post("/api/explore/questions",
                   json={"title": "DML", "sql": "DELETE FROM t", "table_ids": [], "shared": False})
        check("saving a non-SELECT question: 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")

        r = c.post("/api/explore/questions",
                   json={"title": "DML via CTE", "sql": "WITH t2 AS (SELECT 1) INSERT INTO t2 SELECT 1",
                        "table_ids": [], "shared": False})
        check("saving a CTE-prefixed non-SELECT question: 422",
              r.status_code == 422, f"{r.status_code} {r.text[:200]}")

        r = c.post("/api/explore/questions",
                   json={"title": "Unreachable table", "sql": "SELECT 1",
                        "table_ids": ["t_doesnotexist"], "shared": False})
        check("saving a question with an unknown table_id: 422",
              r.status_code == 422, f"{r.status_code} {r.text[:200]}")

        # ── saved questions: CRUD + shared visibility ───────────────────
        r = c.post("/api/explore/questions",
                   json={"title": "Orders by warehouse", "question": "how many orders at warehouse X?",
                        "sql": "SELECT * FROM t", "table_ids": [table_id], "shared": False})
        check("create private question: 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        qid_private = r.json()["id"]
        check("created question is 'mine'", r.json()["mine"] is True, str(r.json()))

        r = c.post("/api/explore/questions",
                   json={"title": "Shared question", "sql": "SELECT 1", "table_ids": [], "shared": True})
        qid_shared = r.json()["id"]

        # a SHARED question naming a real table_id — this is the one a
        # zero-reach viewer must never see (fix for the reach-boundary gap)
        r = c.post("/api/explore/questions",
                   json={"title": "Shared with a table", "question": "salaries by employee",
                        "sql": "SELECT * FROM t", "table_ids": [table_id], "shared": True})
        check("create shared+table-scoped question: 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

        r = c.get("/api/explore/questions")
        titles = {q["title"] for q in r.json()["questions"]}
        check("admin sees all own questions",
              {"Orders by warehouse", "Shared question", "Shared with a table"} <= titles, str(titles))

        admin_id = get_user_id("admin1")
        foreign_id = get_user_id("foreign1")

        # ── reach boundary: foreign1 still has ZERO reach at this point
        # (no permission granted yet, same state that made
        # "foreign user: snapshot is empty" pass above) ──────────────────
        login(c, "foreign1")
        r = c.get("/api/explore/questions")
        check("zero-reach viewer: questions list 200", r.status_code == 200, str(r.status_code))
        titles_zero = {q["title"] for q in r.json()["questions"]}
        check("zero-reach viewer sees the table-less shared question",
              "Shared question" in titles_zero, str(titles_zero))
        check("zero-reach viewer does NOT see the shared question naming an unreachable table",
              "Shared with a table" not in titles_zero, str(titles_zero))
        check("zero-reach viewer does not see the private question either",
              "Orders by warehouse" not in titles_zero, str(titles_zero))

        login(c, "admin1")
        make_permission(dash_id, foreign_id, True)
        login(c, "foreign1")
        r = c.get("/api/explore/questions")
        titles_f = {q["title"] for q in r.json()["questions"]}
        check("once foreign1 gains reach, the table-scoped shared question appears",
              "Shared with a table" in titles_f, str(titles_f))
        check("foreign user sees only the SHARED questions",
              "Shared question" in titles_f and "Orders by warehouse" not in titles_f, str(titles_f))
        mine_flags = {q["title"]: q["mine"] for q in r.json()["questions"]}
        check("shared question is not 'mine' for the foreign viewer",
              mine_flags.get("Shared question") is False, str(mine_flags))

        r = c.delete(f"/api/explore/questions/{qid_shared}")
        check("foreign user cannot delete another user's (even shared) question: 403",
              r.status_code == 403, str(r.status_code))
        r = c.delete(f"/api/explore/questions/{qid_private}")
        check("foreign user cannot delete a private question they can't even see: 403/404",
              r.status_code in (403, 404), str(r.status_code))

        login(c, "admin1")
        r = c.delete(f"/api/explore/questions/{qid_private}")
        check("owner deletes own question: 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        r = c.get("/api/explore/questions")
        titles2 = {q["title"] for q in r.json()["questions"]}
        check("deleted question is gone", "Orders by warehouse" not in titles2, str(titles2))
        r = c.delete(f"/api/explore/questions/{qid_private}")
        check("deleting an already-deleted question: 404", r.status_code == 404, str(r.status_code))

        # ── embed mode: the same page without the app chrome (phase 4) ──
        # `/explore?report=…&mode=chat&embed=1` is what the viewer's Chat
        # pane loads in a same-origin iframe. Two claims are pinned here: the
        # embed branch renders NO sidebar (and the normal page still does),
        # and the three params reach the page only as escaped `#ex-root`
        # data-attributes — never as markup.
        r = c.get("/explore")
        check("plain /explore: 200", r.status_code == 200, str(r.status_code))
        check("plain /explore renders the sidebar", 'class="sidebar"' in r.text)
        check("plain /explore marks itself not-embedded",
              'data-embed=""' in r.text, r.text[:0])

        r = c.get("/explore?embed=1")
        check("/explore?embed=1: 200", r.status_code == 200, str(r.status_code))
        check("/explore?embed=1 renders NO sidebar markup",
              'class="sidebar"' not in r.text)
        check("/explore?embed=1 still renders #ex-root and the chat section",
              'id="ex-root"' in r.text and 'id="ex-chat"' in r.text)
        check("/explore?embed=1 marks the shell as embedded",
              'class="shell ex-embed"' in r.text)
        check("/explore?embed=1 sets data-embed=1", 'data-embed="1"' in r.text)

        r = c.get(f"/explore?report={dash_id}&mode=chat&embed=1")
        check("report/mode land on #ex-root as data-attributes",
              f'data-report="{dash_id}"' in r.text and 'data-mode="chat"' in r.text,
              r.text[r.text.find('id="ex-root"'):][:400])

        r = c.get('/explore?report=%22%3E%3Cscript%3Ex%3C/script%3E&mode=chat')
        check("a hostile `report` is escaped, never markup",
              "<script>x</script>" not in r.text and "&#34;&gt;&lt;script&gt;" in r.text,
              r.text[r.text.find('id="ex-root"'):][:400])

        # ── kill switch: OP_EXPLORE_ENABLED=false -> 404 everywhere ─────
        settings.explore_enabled = False
        try:
            check("catalog snapshot 404s when disabled",
                  c.get("/api/explore/catalog").status_code == 404)
            check("nl2sql 404s when disabled",
                  c.post("/api/explore/nl2sql",
                        json={"question": "x", "table_ids": [table_id]}).status_code == 404)
            check("questions list 404s when disabled",
                  c.get("/api/explore/questions").status_code == 404)
        finally:
            settings.explore_enabled = True
        check("re-enabled: catalog works again", c.get("/api/explore/catalog").status_code == 200)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
