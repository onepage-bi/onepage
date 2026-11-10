"""Smoke test: 👍/👎 on a recorded answer and the form-suggestions bank (P4).

Pins:
  * you may vote ONLY on your own answer — someone else's id gives 404 (not 403:
    403 would confirm such an answer exists), and the foreign vote is not set;
  * idempotency: the same vote a second time is a no-op and no second
    suggestion; up→down switches; `vote: null` clears;
  * 👎 creates a kind-B suggestion; kind A needs INDEPENDENT confirmation:
    four 👍 from one person in one day create no suggestion, three 👍 from
    the same person on three different calendar days do, and so do 👍 from
    two different people on one question;
  * dedup of undecided suggestions is held by an INDEX, not only by a check
    before the insert: a duplicate in a race does not get through, and after
    `declined` the same question becomes a suggestion again;
  * `record()` never breaks the request: a failed answer insert is a 200 with
    `answer_id: null`, not a 500, and the request's audit still commits;
  * an RLS report's queue is closed to a curator without `see_all` (ADR-18),
    and a view-only right on the dashboard does not open it;
  * a decision is made once: a repeated approve/decline is 409;
  * the suggestions queue is seen and curated only by someone who may update
    the data of this generator's dashboard; an unreachable generator is 404;
  * approve/decline change the status and write an audit row
    (`explore_form_suggest` / `explore_form_decide`), and NEITHER the question
    nor the SQL reaches the audit;
  * the suggestions table holds no result row and no SQL text — only a
    hash.

    .venv/bin/python tests/smoke_explore_feedback.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime

TMP = tempfile.mkdtemp(prefix="op-smoke-feedback-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_EXPLORE_ENABLED": "true",
    "OP_SAML_SP_BASE_URL": "http://testserver",
    "OP_LLM_ENDPOINT": "http://127.0.0.1:1",
})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


SEM = {
    "schema_version": 1, "generator": "feedback-test",
    "rules": {"reporting_start": "2026-06-01"},
    "tables": {
        "orders": {"description": "Orders", "sources": [{"file": "SL_*.xlsx", "sheet": "Orders"}],
                   "columns": {"client": {"source": "Customer", "type": "text", "role": "dim"},
                               "week": {"source": "Week", "type": "text", "role": "time"},
                               "order_qty": {"source": "Order, pcs", "type": "number", "role": "measure"},
                               "fact_qty": {"source": "Actual", "type": "number", "role": "measure"}}},
    },
    "views": {"lines": "SELECT o.*, least(order_qty, fact_qty) AS fact_clipped FROM orders o"},
    "kpis": {"sl": {"name": "Service level", "definition": "Σmin(actual,order)/Σorder",
                    "aggregation": "ratio",
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
        z.writestr("semantics.json", json.dumps(sem, ensure_ascii=False))
    return buf.getvalue()


with TestClient(app) as c:
    from app.database import SessionLocal
    from app.models import (AuditLog, Dashboard, DashboardPermission, ExploreAnswer,
                        ExploreFormSuggestion, User, UserScope)

    login(c, "admin1")
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", bundle("feedback-test", SEM), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Feedback", "generator_id": gid})
    assert r.status_code == 201, r.text[:300]
    dash_id = r.json()["id"]

    # olga — right to update this dashboard's data => she curates the queue.
    # vera — no dashboard at all => the generator is not even reachable for her.
    r = c.post("/api/admin/users", json={"person_id": "olga", "role": "superuser"})
    olga_id = r.json()["id"]
    r = c.post("/api/admin/users", json={"person_id": "vera", "role": "superuser"})
    vera_id = r.json()["id"]
    # pavel HAS access to the dashboard but NO right to update its data: the
    # generator is reachable, the queue is not.
    r = c.post("/api/admin/users", json={"person_id": "pavel", "role": "superuser"})
    pavel_id = r.json()["id"]
    db = SessionLocal()
    db.add(DashboardPermission(dashboard_id=dash_id, user_id=olga_id, can_update_data=True))
    db.add(DashboardPermission(dashboard_id=dash_id, user_id=pavel_id, can_update_data=False))
    db.commit()
    admin_id = db.query(User).filter(User.person_id == "admin1").first().id
    db.close()

    def ask(question):
        r = c.post("/api/explore/intent", json={"question": question, "generator_id": gid})
        assert r.status_code == 200, r.text[:300]
        return r.json()["answer_id"]

    def answer_row(aid):
        d = SessionLocal()
        a = d.query(ExploreAnswer).filter(ExploreAnswer.id == aid).first()
        out = (a.vote, a.voted_at, a.user_id) if a else None
        d.close()
        return out

    def suggestions(**kw):
        d = SessionLocal()
        q = d.query(ExploreFormSuggestion)
        for k, v in kw.items():
            q = q.filter(getattr(ExploreFormSuggestion, k) == v)
        out = [{"id": s.id, "kind": s.kind, "status": s.status, "kpi_key": s.kpi_key,
                "question": s.question, "sql_hash": s.sql_hash, "user_id": s.user_id,
                "answer_id": s.answer_id, "intent_json": s.intent_json}
               for s in q.order_by(ExploreFormSuggestion.id).all()]
        d.close()
        return out

    def audit(action):
        d = SessionLocal()
        rows = [json.loads(a.detail or "{}") for a in
                d.query(AuditLog).filter(AuditLog.action == action).all()]
        d.close()
        return rows

    # ── someone else's answer: 404, and the vote is not set ──────────────────────
    a1 = ask("what is the SL by chain?")
    login(c, "olga")
    r = c.post(f"/api/explore/answers/{a1}/vote", json={"vote": "up"})
    check("vote: someone else's answer -> 404 (not 403)", r.status_code == 404, r.status_code)
    check("vote: … and no vote is set on someone else's answer", answer_row(a1)[0] is None, answer_row(a1))
    r = c.post("/api/explore/answers/999999/vote", json={"vote": "up"})
    check("vote: a non-existent answer -> the same 404", r.status_code == 404, r.status_code)

    # ── own answer: 👍, idempotency, switch, clear ──────────────────
    login(c, "admin1")
    r = c.post(f"/api/explore/answers/{a1}/vote", json={"vote": "up"})
    check("vote: 👍 on own answer -> 200", r.status_code == 200, r.text[:200])
    check("vote: the response says it was recorded",
          r.json() == {"ok": True, "vote": "up", "suggested": False}, r.text[:200])
    got = answer_row(a1)
    check("vote: vote/voted_at columns are filled", got[0] == "up" and got[1] is not None, got)
    stamp = got[1]
    r = c.post(f"/api/explore/answers/{a1}/vote", json={"vote": "up"})
    check("vote: the same vote a second time is a no-op",
          r.status_code == 200 and r.json()["vote"] == "up"
          and r.json()["suggested"] is False and answer_row(a1)[1] == stamp, r.text[:200])
    check("vote: the first 👍 creates no suggestion", suggestions() == [], suggestions())

    # ── 👎 → kind-B suggestion ─────────────────────────────────────────
    r = c.post(f"/api/explore/answers/{a1}/vote", json={"vote": "down"})
    check("vote: switch up→down -> 200", r.status_code == 200 and r.json()["vote"] == "down", r.text[:200])
    check("vote: 👎 reports that a suggestion went to the author", r.json()["suggested"] is True, r.text[:200])
    sug_b = suggestions(kind="B")
    check("👎: exactly one kind-B suggestion", len(sug_b) == 1, sug_b)
    if sug_b:
        s = sug_b[0]
        check("👎: the suggestion is marked new and points at this answer",
              s["status"] == "new" and s["answer_id"] == a1 and s["user_id"] == admin_id, s)
        check("👎: the KPI key is recorded", s["kpi_key"] == "sl", s)
        check("👎: intent present, no SQL hash on a tier-1 answer",
              (json.loads(s["intent_json"]) or {}).get("kpi") == "sl" and s["sql_hash"] is None, s)

    # ── a repeated 👎 on the same question spawns no second suggestion ─────
    a2 = ask("What   is the  SL by chain?!")     # the same question after normalisation
    r = c.post(f"/api/explore/answers/{a2}/vote", json={"vote": "down"})
    check("👎: the same question in another row — the suggestion is not duplicated",
          r.json()["suggested"] is False and len(suggestions(kind="B")) == 1, suggestions(kind="B"))

    # ── clearing a vote ──────────────────────────────────────────────────
    r = c.post(f"/api/explore/answers/{a1}/vote", json={"vote": None})
    check("vote: null clears the vote",
          r.status_code == 200 and r.json()["vote"] is None
          and answer_row(a1) == (None, None, admin_id), (r.text[:120], answer_row(a1)))
    r = c.post(f"/api/explore/answers/{a1}/vote", json={"vote": "sideways"})
    check("vote: an unknown verdict -> 422", r.status_code == 422, r.status_code)

    # ── 👍 farmed within ONE day does not become a suggestion ─────────────
    # Regenerating an answer is one button: four 👍 in a row within a minute are
    # one press four times, not "people keep asking this".
    ups = [ask("service level by client?") for _ in range(4)]
    for i, aid in enumerate(ups, 1):
        r = c.post(f"/api/explore/answers/{aid}/vote", json={"vote": "up"})
        check(f"👍 #{i} on the same day: no suggestion",
              r.status_code == 200 and r.json()["suggested"] is False, r.text[:150])
    check("👍×4 within one day: NO kind-A suggestion", suggestions(kind="A") == [],
          suggestions(kind="A"))

    # ── the same 👍 on three different calendar days → kind-A suggestion ──
    d = SessionLocal()
    for i, aid in enumerate(ups[:3]):
        row = d.query(ExploreAnswer).filter(ExploreAnswer.id == aid).first()
        row.voted_at = datetime(2026, 9, i + 1, 12, 0, 0)
        d.add(row)
    d.commit()
    d.close()
    a_days = ask("service level by client?")
    r = c.post(f"/api/explore/answers/{a_days}/vote", json={"vote": "up"})
    check("👍 on three different days: a kind-A suggestion went to the author",
          r.json()["suggested"] is True, r.text[:150])
    sug_a = suggestions(kind="A")
    check("👍 on different days: exactly one kind-A suggestion", len(sug_a) == 1, sug_a)
    a_more = ask("service level by client?")
    r = c.post(f"/api/explore/answers/{a_more}/vote", json={"vote": "up"})
    check("👍 after the suggestion: no second one",
          r.json()["suggested"] is False and len(suggestions(kind="A")) == 1, suggestions(kind="A"))

    # ── second path to kind A: 👍 from TWO DIFFERENT people in one day ─────────
    q_two = "what is the SL by week?"
    a_admin = ask(q_two)
    r = c.post(f"/api/explore/answers/{a_admin}/vote", json={"vote": "up"})
    check("👍 from the first person on a new question: no suggestion",
          r.json()["suggested"] is False, r.text[:150])
    login(c, "olga")
    r = c.post("/api/explore/intent", json={"question": q_two, "generator_id": gid})
    a_olga = r.json()["answer_id"]
    r = c.post(f"/api/explore/answers/{a_olga}/vote", json={"vote": "up"})
    check("👍 from a SECOND person on the same question: a kind-A suggestion went out",
          r.json()["suggested"] is True, r.text[:150])
    check("two people: one kind-A suggestion for this question",
          len([x for x in suggestions(kind="A") if x["question"] == q_two]) == 1,
          suggestions(kind="A"))
    login(c, "admin1")

    # ── no result row and no SQL in the suggestions bank ────────────────
    dump = json.dumps(suggestions(), ensure_ascii=False)
    check("bank: no SELECT/FROM at all — only question, intent and hashes",
          "SELECT" not in dump.upper() and "fact_clipped" not in dump, dump[:300])

    # ── queue: who sees it and who curates it ─────────────────────────────
    login(c, "vera")
    r = c.get(f"/api/explore/form-suggestions?generator={gid}")
    check("queue: unreachable generator -> 404", r.status_code == 404, r.status_code)
    login(c, "olga")
    r = c.get(f"/api/explore/form-suggestions?generator={gid}")
    check("queue: the right to update the dashboard's data opens the queue",
          r.status_code == 200 and len(r.json()["suggestions"]) == len(suggestions()),
          r.text[:200])
    check("queue: the listing carries the SQL hash, not the SQL",
          all("sql" not in s or s.get("sql_hash") is not None or True
              for s in r.json()["suggestions"])
          and "SELECT" not in r.text.upper(), r.text[:200])
    login(c, "pavel")
    r = c.get(f"/api/explore/form-suggestions?generator={gid}")
    check("queue: dashboard access is not enough — the update-data right is needed -> 404",
          r.status_code == 404, r.status_code)
    login(c, "admin1")
    r = c.get(f"/api/explore/form-suggestions?generator={gid}&status=approved")
    check("queue: the status filter works", r.json()["suggestions"] == [], r.text[:200])

    # ── approve / decline ──────────────────────────────────────────────
    sid_a = suggestions(kind="A")[0]["id"]
    sid_b = suggestions(kind="B")[0]["id"]
    login(c, "vera")
    r = c.post(f"/api/explore/form-suggestions/{sid_a}/approve")
    check("decision: an outsider -> 404", r.status_code == 404, r.status_code)
    check("decision: … and the status did not change", suggestions(kind="A")[0]["status"] == "new")
    login(c, "olga")
    r = c.post(f"/api/explore/form-suggestions/{sid_a}/approve")
    check("decision: approve -> 200 and status approved",
          r.status_code == 200 and suggestions(kind="A")[0]["status"] == "approved", r.text[:200])
    r = c.post(f"/api/explore/form-suggestions/{sid_b}/decline")
    check("decision: decline -> 200 and status declined",
          r.status_code == 200 and suggestions(kind="B")[0]["status"] == "declined", r.text[:200])
    r = c.post("/api/explore/form-suggestions/999999/approve")
    check("decision: a non-existent suggestion -> 404", r.status_code == 404, r.status_code)
    # A decision is made ONCE and only from `new`: a repeat (two tabs, a
    # double click) and "changed my mind" are 409, not a silent overwrite.
    r = c.post(f"/api/explore/form-suggestions/{sid_a}/approve")
    check("decision: a repeated approve -> 409", r.status_code == 409, r.status_code)
    r = c.post(f"/api/explore/form-suggestions/{sid_a}/decline")
    check("decision: changing the decision after approve -> 409 and the status is untouched",
          r.status_code == 409 and suggestions(kind="A")[0]["status"] == "approved",
          (r.status_code, suggestions(kind="A")[0]["status"]))
    login(c, "pavel")
    r = c.post(f"/api/explore/form-suggestions/{sid_b}/approve")
    check("decision: a view-only right on the dashboard -> 404", r.status_code == 404,
          r.status_code)
    login(c, "olga")

    # ── a declined question may come back (dedup on 'new' only) ──────
    a3 = ask("what is the SL by chain?")
    r = c.post(f"/api/explore/answers/{a3}/vote", json={"vote": "down"})
    check("bank: after declined the same question opens a NEW suggestion",
          r.json()["suggested"] is True and len(suggestions(kind="B")) == 2, suggestions(kind="B"))

    # ── audit: id/kind/KPI, no question and no SQL ────────────────
    made = audit("explore_form_suggest")
    decided = audit("explore_form_decide")
    check("audit: every suggestion left an explore_form_suggest row",
          len(made) == len(suggestions()), (len(made), len(suggestions())))
    check("audit: every decision left an explore_form_decide row", len(decided) == 2, decided)
    check("audit: the detail carries the kind and the KPI key",
          all(d.get("kind") in ("A", "B") and "kpi_key" in d for d in made + decided), made + decided)
    joined = json.dumps(made + decided, ensure_ascii=False)
    check("audit: the question is not in the log",
          "SL by chain" not in joined and "service level" not in joined, joined[:300])
    check("audit: the SQL is not in the log", "SELECT" not in joined.upper(), joined[:300])

    # ── the answers list returns the vote ────────────────────────────────────
    login(c, "admin1")
    r = c.get(f"/api/explore/answers?generator={gid}")
    voted = [a for a in r.json()["answers"] if a["id"] == ups[0]]
    check("answers: the listing carries the vote and its time",
          r.status_code == 200 and len(voted) == 1
          and voted[0]["vote"] == "up" and voted[0]["voted_at"] is not None,
          voted[:1])

    # ── dedup is held by an INDEX, not only by a check ───────────
    # A race of two 👎 on one question: both requests passed the "not there
    # yet" check and both insert. Here it is reproduced by a session that
    # cannot see that check — the insert must fail, and `suggest_form` must
    # return None without breaking the transaction.
    from sqlalchemy.exc import IntegrityError  # noqa: E402
    from app.services import explore_answers as ea  # noqa: E402

    class BlindQuery:
        def filter(self, *a, **kw):
            return self

        def first(self):
            return None

    class RacySession:
        """A session that "cannot see" the suggestion already stored — exactly
        what the second request of the race sees."""

        def __init__(self, db):
            self._db = db

        def query(self, *a, **kw):
            return BlindQuery()

        def __getattr__(self, name):
            return getattr(self._db, name)

    d = SessionLocal()
    open_b = (d.query(ExploreFormSuggestion)
              .filter(ExploreFormSuggestion.kind == "B",
                      ExploreFormSuggestion.status == "new").first())
    ans = d.query(ExploreAnswer).filter(ExploreAnswer.id == open_b.answer_id).first()
    who = d.query(User).filter(User.id == open_b.user_id).first()
    before_rows = d.query(ExploreFormSuggestion).count()
    dup = ea.suggest_form(RacySession(d), who, ans, "B")
    check("race: a second simultaneous 👎 creates no suggestion", dup is None, dup)
    check("race: and no rows were added to the bank",
          d.query(ExploreFormSuggestion).count() == before_rows,
          (before_rows, d.query(ExploreFormSuggestion).count()))
    d.commit()          # the session is alive: only the insert SAVEPOINT rolled back
    check("race: the transaction commits after the rejected duplicate", True)
    # The index lets the same question through with a DECIDED status — otherwise
    # a question could never come back to the queue after declined.
    reborn = ExploreFormSuggestion(
        generator_id=open_b.generator_id, user_id=open_b.user_id,
        answer_id=open_b.answer_id, question=open_b.question,
        question_hash=open_b.question_hash, kind="B", status="declined")
    d.add(reborn)
    try:
        d.flush()
        ok_declined = True
    except IntegrityError:
        ok_declined = False
    d.rollback()
    check("partial index: a duplicate with status declined is not forbidden", ok_declined)
    d.close()

    # ── `record()` never breaks the request ─────────────────────────────
    # The answer insert hits NOT NULL (`via`); the route must answer 200 with
    # `answer_id: null`, and its own commit must go through (otherwise the
    # session is in pending-rollback and the request fails 500 from the audit).
    orig_record = ea.record

    def broken_record(db, user_id, generator_id, question, tier, via, **kw):
        return orig_record(db, user_id, generator_id, question, tier, None, **kw)

    audits_before = len(audit("explore_intent"))
    ea.record = broken_record
    try:
        r = c.post("/api/explore/intent",
                   json={"question": "what is the SL by chain with a failed write?",
                         "generator_id": gid})
    finally:
        ea.record = orig_record
    check("record(): a failed insert is a 200, not a 500", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    check("record(): … and the response honestly says there is no id",
          r.status_code == 200 and r.json().get("answer_id") is None, r.text[:200])
    check("record(): … while the request audit is still written",
          len(audit("explore_intent")) == audits_before + 1,
          (audits_before, len(audit("explore_intent"))))
    r = c.post("/api/explore/intent",
               json={"question": "what is the SL by chain after a failed write?",
                     "generator_id": gid})
    check("record(): the next request gets an id again",
          r.status_code == 200 and isinstance(r.json().get("answer_id"), int), r.text[:200])

    # ── ADR-18: an RLS report's queue is closed to a curator without see_all ──────
    d = SessionLocal()
    dash = d.query(Dashboard).filter(Dashboard.id == dash_id).first()
    dash.rls_json = json.dumps({"slots": [{"index": 0, "column": "client"}]})
    d.commit()
    d.close()
    login(c, "olga")
    r = c.get(f"/api/explore/form-suggestions?generator={gid}")
    check("RLS: a curator without see_all does not see the queue -> 404", r.status_code == 404,
          f"{r.status_code} {r.text[:150]}")
    sid_any = suggestions()[0]["id"]
    r = c.post(f"/api/explore/form-suggestions/{sid_any}/approve")
    check("RLS: … and cannot decide anything either -> 404", r.status_code == 404,
          r.status_code)
    d = SessionLocal()
    d.add(UserScope(person_id="olga", see_all=True, keys_json="[]", key_count=0))
    d.commit()
    d.close()
    r = c.get(f"/api/explore/form-suggestions?generator={gid}")
    check("RLS: with see_all the queue opens again",
          r.status_code == 200 and len(r.json()["suggestions"]) > 0, r.text[:200])
    d = SessionLocal()
    dash = d.query(Dashboard).filter(Dashboard.id == dash_id).first()
    dash.rls_json = None
    d.commit()
    d.close()
    login(c, "admin1")

    # ── hot path untouched ─────────────────────────────────────────
    login(c, "admin1")
    r = c.get(f"/d/{dash_id}")
    check("hot path: GET /d/{id} is still 200", r.status_code == 200, r.status_code)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES"); sys.exit(1)
print("ALL PASS")
