"""Smoke test for the "Quests" API router (app/api/quests.py, phase 5) —
routes only: the underlying game logic is already pinned by
tests/smoke_quests_{calendar,streak,rng,verify,quiz,config,share_audit}.py.
This test checks the HTTP surface: the enabled-flag gate, status codes on
each error branch, audit rows, and the admin config/quiz-import endpoints.

Boot idiom from tests/smoke_favorites.py; holiday override + `_HOLIDAYS_OVERRIDE`
idiom from tests/smoke_quests_verify.py, for a deterministic calendar.
"""
import os
import sys
import tempfile
from datetime import date

TMP = tempfile.mkdtemp(prefix="op-quests-api-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_QUESTS_ENABLED": "true",
})
sys.path.insert(0, os.getcwd())
from pathlib import Path  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog, QuestCard, QuestOpening, QuizAttempt, QuizQuestion, User,
)
from app.services import quests as qsvc  # noqa: E402
from app.services import quests_config  # noqa: E402

FAILURES = []
qsvc._HOLIDAYS_OVERRIDE = set()   # deterministic calendar, mirrors smoke_quests_verify.py

# Surprise of the day and the quiz run on workdays only: on a weekend the test looks
# at the nearest workday, otherwise it would fail every Saturday.
if not qsvc.is_working(qsvc.today()):
    qsvc._TODAY_OVERRIDE = qsvc.next_working(qsvc.today())


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def audit_count():
    db = SessionLocal()
    try:
        return db.query(AuditLog).count()
    finally:
        db.close()


def set_card_count(person_id, card_id, n):
    db = SessionLocal()
    try:
        me = db.query(User).filter(User.person_id == person_id).first()
        card = db.query(QuestCard).filter(
            QuestCard.user_id == me.id, QuestCard.card_id == card_id).first()
        if card is None:
            card = QuestCard(user_id=me.id, card_id=card_id, count=0)
            db.add(card)
        card.count = n
        db.commit()
        return me.id
    finally:
        db.close()


def card_count(person_id, card_id):
    db = SessionLocal()
    try:
        me = db.query(User).filter(User.person_id == person_id).first()
        card = db.query(QuestCard).filter(
            QuestCard.user_id == me.id, QuestCard.card_id == card_id).first()
        return int(card.count or 0) if card is not None else None
    finally:
        db.close()


def last_audit(action):
    db = SessionLocal()
    try:
        return (db.query(AuditLog).filter(AuditLog.action == action)
                .order_by(AuditLog.id.desc()).first())
    finally:
        db.close()


def main():
    with TestClient(app) as c:
        login(c, "admin1")

        r = c.post("/api/admin/users", json={
            "person_id": "vis1", "role": "visitor", "display_name": "Vis One"})
        check("second (visitor) user created", r.status_code == 200 and r.json().get("ok") is True,
              f"{r.status_code} {r.text[:200]}")

        # ── flag off: user routes 404, admin config still reachable ────────
        touched = quests_config.set_overrides({"quests_enabled": False})
        check("set_overrides disabled the flag", "quests_enabled" in touched, touched)

        r = c.get("/api/quests/state")
        check("flag off: /api/quests/state 404", r.status_code == 404, f"{r.status_code} {r.text}")
        r = c.get("/quests")
        check("flag off: /quests 404", r.status_code == 404, f"{r.status_code}")
        r = c.get("/api/admin/quests/config")
        check("flag off: admin config GET still 200 (admin must be able to re-enable it)",
              r.status_code == 200, f"{r.status_code} {r.text}")
        if Path("templates/pages/admin_quests.html").exists():
            r = c.get("/admin/quests")
            check("flag off: /admin/quests page still reachable (not gated by the flag)",
                  r.status_code == 200, f"{r.status_code}")
        else:
            print("SKIP  /admin/quests render check: templates/pages/admin_quests.html not present yet")

        quests_config.set_overrides({}, reset=["quests_enabled"])
        check("reset restored the env default (enabled)", quests_config.get()["enabled"] is True,
              quests_config.get())

        # ── badge before/after open ─────────────────────────────────────
        r = c.get("/api/quests/badge")
        check("badge before open: open=False", r.status_code == 200 and r.json()["open"] is False, r.text)

        r = c.post("/api/quests/open")
        check("open: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        body = r.json() if r.status_code == 200 else {}
        check("open: opening present", bool(body.get("opening")), body)
        audit = last_audit("quests_open")
        detail = audit.get_detail() if audit else {}
        check("open: quests_open audit row with seed/roll/weights",
              audit is not None and "seed" in detail and "roll" in detail and "weights" in detail,
              detail)

        r = c.get("/api/quests/badge")
        check("badge after open: open=True", r.status_code == 200 and r.json()["open"] is True, r.text)

        r = c.post("/api/quests/open")
        check("open twice: 409", r.status_code == 409, f"{r.status_code} {r.text}")

        # ── exchange ─────────────────────────────────────────────────────
        # Exchange works only within the ACTIVE season, so duplicates come from
        # its set; a card of the frozen season 1 is checked separately.
        # Set the active season ourselves (via the same overlay the admin panel uses):
        # the test must not depend on which season is the default.
        # Season 2 active -> season 1 is "closed"; the overlay is reset at the end of the block.
        quests_config.set_overrides({"quests_season": 2})
        DUP = qsvc.season_card_ids(2)[0]
        frozen = qsvc.season_card_ids(1)[0]
        set_card_count("admin1", frozen, 5)
        r = c.post("/api/quests/exchange", json={"card_id": frozen})
        check("exchange of a closed-season card: 400", r.status_code == 400,
              f"{r.status_code} {r.text}")
        check("closed-season exchange deducted nothing",
              card_count("admin1", frozen) == 5, card_count("admin1", frozen))
        r = c.post("/api/quests/exchange", json={"card_id": DUP})
        check("exchange <5: 400", r.status_code == 400, f"{r.status_code} {r.text}")

        set_card_count("admin1", DUP, 5)

        r = c.post("/api/quests/exchange", json={"card_id": DUP})
        check("exchange ==5: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        ex = r.json() if r.status_code == 200 else {}
        # The exchange debits 5 and immediately rolls a new card, which may
        # BE the same card — `remaining` is the honest post-everything count.
        won = (ex.get("opening") or {}).get("card_id")
        check("exchange: five debited (remaining 0, or 1 if the roll returned it)",
              ex.get("remaining") == (1 if won == DUP else 0), (ex.get("remaining"), won))

        # atomic debit: a second exchange has nothing left to spend
        r = c.post("/api/quests/exchange", json={"card_id": DUP})
        check("exchange again with <5 left: 400", r.status_code == 400, f"{r.status_code} {r.text}")

        # pending surprises: the exchanged card waits unrevealed in the hero;
        # /open reveals it even though today's daily is already opened, and
        # once the queue is empty /open is back to 409.
        st = c.get("/api/quests/state").json()
        check("exchange leaves a pending surprise", len(st.get("pending") or []) == 1
              and st["pending"][0]["revealed"] is False, st.get("pending"))
        r = c.post("/api/quests/open")
        check("open reveals the pending card", r.status_code == 200 and r.json().get("kind") == "pending"
              and r.json().get("pending_left") == 0, f"{r.status_code} {r.text[:200]}")
        st = c.get("/api/quests/state").json()
        check("pending queue drained", (st.get("pending") or []) == [])
        r = c.post("/api/quests/open")
        check("open with empty queue: 409", r.status_code == 409, f"{r.status_code}")

        # race simulation: the `dup:n` slot this exchange would take is already
        # occupied → 409 AND the five cards stay on the account (no negative,
        # no silent debit).
        db = SessionLocal()
        try:
            me = db.query(User).filter(User.person_id == "admin1").first()
            used = db.query(QuestOpening).filter(
                QuestOpening.user_id == me.id,
                QuestOpening.day == qsvc.today(),
                QuestOpening.source.like("dup:%")).count()
            # The service counts today's `dup:` rows AFTER its own debit and
            # takes the next number — with this decoy in place that number is
            # `dup:{used+2}`, i.e. exactly the row we just inserted.
            db.add(QuestOpening(user_id=me.id, day=qsvc.today(),
                                source=f"dup:{used + 2}", card_id=DUP,
                                rarity="common", seed="raceseed", weights_json="[]",
                                roll=0.5))
            db.commit()
        finally:
            db.close()
        set_card_count("admin1", DUP, 5)
        r = c.post("/api/quests/exchange", json={"card_id": DUP})
        check("exchange into an occupied dup:n slot: 409", r.status_code == 409,
              f"{r.status_code} {r.text}")
        check("exchange race: the five cards were not debited",
              card_count("admin1", DUP) == 5, card_count("admin1", DUP))
        quests_config.set_overrides({}, reset=["quests_season"])  # restore the previous state

        # ── client event ─────────────────────────────────────────────────
        r = c.post("/api/quests/event", json={"kind": "theme"})
        check("event first time: not already", r.status_code == 200 and r.json().get("already") is False, r.text)
        state_before = c.get("/api/quests/state").json()
        r2 = c.post("/api/quests/event", json={"kind": "theme"})
        check("event second time: already=True", r2.status_code == 200 and r2.json().get("already") is True, r2.text)
        state_after = c.get("/api/quests/state").json()
        check("event: points +10 only once", state_after["points"] == state_before["points"], (
            state_before["points"], state_after["points"]))

        # ── GET /state writes nothing ────────────────────────────────────
        before_rows = audit_count()
        r = c.get("/api/quests/state")
        st_body = r.json() if r.status_code == 200 else {}
        check("state GET: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        check("state GET wrote no audit rows", audit_count() == before_rows,
              (before_rows, audit_count()))
        check("state GET awards is empty (no verification on a read)",
              st_body.get("awards") == [], st_body.get("awards"))
        check("state GET still carries leaderboard/quiz/config",
              {"leaderboard", "quiz", "config"} <= set(st_body), sorted(st_body))

        # ── POST /verify is where awards come from now ───────────────────
        r = c.post("/api/quests/verify")
        check("verify POST: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        check("verify POST returns awards", isinstance(r.json().get("awards"), list), r.text)

        # ── quiz ─────────────────────────────────────────────────────────
        r = c.post("/api/quests/quiz/start")
        check("quiz start: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        qbody = r.json() if r.status_code == 200 else {}
        token = qbody.get("token")
        q0 = qbody.get("question") or {}
        check("quiz start: 3-question session", qbody.get("total") == 3, qbody)
        check("quiz question hides answer_idx/explanation",
              "answer_idx" not in q0 and "explanation" not in q0, q0)
        check("quiz start does not start the timer",
              qbody.get("served") is False and qbody.get("remaining_sec") is None, qbody)
        audit = last_audit("quests_quiz_start")
        check("quiz start: quests_quiz_start audit row", audit is not None,
              "no audit row")

        # resuming before begin() keeps served=False
        r = c.post("/api/quests/quiz/start")
        check("quiz start resumed: still not served",
              r.status_code == 200 and r.json().get("served") is False, r.text)

        r = c.post("/api/quests/quiz/begin", json={"token": "nope"})
        check("quiz begin unknown token: 404", r.status_code == 404, f"{r.status_code} {r.text}")

        r = c.post("/api/quests/quiz/begin", json={"token": token})
        check("quiz begin: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        b1 = r.json() if r.status_code == 200 else {}
        check("quiz begin: index/question/served_at/remaining_sec/score/opponent",
              {"index", "question", "served_at", "remaining_sec", "score", "opponent"}
              <= set(b1), sorted(b1))
        check("quiz begin: remaining_sec == the question's sec",
              b1.get("remaining_sec") == (b1.get("question") or {}).get("sec"), b1)
        r = c.post("/api/quests/quiz/begin", json={"token": token})
        b2 = r.json() if r.status_code == 200 else {}
        check("quiz begin is idempotent: same served_at",
              b2.get("served_at") == b1.get("served_at"), (b1.get("served_at"), b2.get("served_at")))

        r = c.post("/api/quests/quiz/start")
        check("quiz start after begin: served=true with an int remainder",
              r.status_code == 200 and r.json().get("served") is True
              and isinstance(r.json().get("remaining_sec"), int), r.text)

        r = c.post("/api/quests/quiz/answer", json={"token": token, "pick": 0, "index": 5})
        check("quiz answer wrong index: 409", r.status_code == 409, f"{r.status_code} {r.text}")

        done = False
        summary = None
        nxt = None
        for idx in range(3):
            r = c.post("/api/quests/quiz/answer", json={"token": token, "pick": 0, "index": idx})
            check(f"quiz answer index {idx}: 200", r.status_code == 200, f"{r.status_code} {r.text}")
            payload = r.json() if r.status_code == 200 else {}
            done = payload.get("done")
            summary = payload.get("summary")
            if idx < 2:
                nxt = payload.get("next") or {}
                # 2026-09-10: the next question is handed out UNSTARTED —
                # its clock starts at /quiz/begin, after the explanation.
                check(f"quiz answer {idx}: next is unstarted, remaining_sec == sec",
                      not nxt.get("served_at") and nxt.get("served") is False
                      and nxt.get("remaining_sec") == nxt.get("sec"), nxt)
                rb = c.post("/api/quests/quiz/begin", json={"token": token})
                check(f"quiz begin for question {idx + 1}: 200 and full window",
                      rb.status_code == 200 and rb.json().get("index") == idx + 1
                      and rb.json().get("remaining_sec") == nxt.get("sec"),
                      f"{rb.status_code} {rb.text[:200]}")
        check("quiz: last answer marks done", done is True, done)
        check("quiz: summary present on done", isinstance(summary, dict), summary)

        db = SessionLocal()
        try:
            rows = (db.query(AuditLog).filter(AuditLog.action == "quests_quiz_answer")
                    .order_by(AuditLog.id).all())
            details = [row.get_detail() for row in rows]
        finally:
            db.close()
        check("quiz: one quests_quiz_answer audit row per answer", len(details) == 3, len(details))
        check("quiz answer audit detail shape",
              all({"index", "correct", "pts", "elapsed_ms"} <= set(d) for d in details), details)
        check("quiz answer audit carries no question text",
              all("q" not in d and "question" not in d for d in details), details)

        r = c.post("/api/quests/quiz/start")
        check("quiz start again same day: 409", r.status_code == 409, f"{r.status_code} {r.text}")
        r = c.post("/api/quests/quiz/begin", json={"token": token})
        check("quiz begin on a finished attempt: 409", r.status_code == 409,
              f"{r.status_code} {r.text}")

        # ── leaderboard opt-in ───────────────────────────────────────────
        r = c.put("/api/quests/leaderboard-optin", json={"optin": True})
        check("leaderboard optin true: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        check("leaderboard optin true: rows present", "rows" in r.json(), r.json())
        audit = last_audit("quests_optin")
        check("leaderboard optin: quests_optin audit row", audit is not None, "no audit row")

        # ── admin config ─────────────────────────────────────────────────
        login(c, "vis1")
        r = c.put("/api/admin/quests/config", json={"season_length": 30})
        check("admin config PUT as non-admin: 403", r.status_code == 403, f"{r.status_code} {r.text}")

        login(c, "admin1")
        r = c.put("/api/admin/quests/config", json={"season_start": "not-a-date"})
        check("admin config PUT bad date: 400", r.status_code == 400, f"{r.status_code} {r.text}")

        r = c.put("/api/admin/quests/config", json={"season_length": 45})
        check("admin config PUT valid: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        check("admin config PUT valid: season_length applied",
              r.status_code == 200 and r.json().get("season_length") == 45, r.text)
        audit = last_audit("quests_admin_config")
        check("admin config PUT: quests_admin_config audit row (names only)",
              audit is not None and audit.get_detail().get("changed") == ["quests_season_length"],
              audit.get_detail() if audit else None)

        # ── quiz import (dry run doesn't write) ─────────────────────────
        # A dry run of the UNCHANGED bank would look identical whether it
        # wrote or not, so the check imports a MODIFIED bank and then reads
        # the stored row back: the marker must never reach the table.
        from app.services import quests_quiz as qquiz  # noqa: PLC0415

        bank = qquiz.load_default_bank()
        qid = int(bank[0]["id"])
        db = SessionLocal()
        try:
            stored_before = db.get(QuizQuestion, qid)
            text_before = stored_before.question if stored_before is not None else None
            opts_before = stored_before.get_options() if stored_before is not None else None
        finally:
            db.close()
        check("bank row present before the dry run", bool(text_before), text_before)

        bank[0]["q"] = "DRYRUN-MARKER " + bank[0]["q"][:100]
        bank[0]["o"] = ["DRYRUN-" + o[:60] for o in bank[0]["o"]]
        before = c.get("/api/admin/quests/config").json()["bank"]
        r = c.post("/api/admin/quests/quiz-import", json={"bank": bank, "dry_run": True})
        check("quiz-import dry_run of a modified bank: 200", r.status_code == 200,
              f"{r.status_code} {r.text[:300]}")
        check("quiz-import dry_run: report says dry_run",
              r.status_code == 200 and r.json().get("dry_run") is True, r.text[:200])
        after = c.get("/api/admin/quests/config").json()["bank"]
        check("quiz-import dry_run: bank_status unchanged", before == after, (before, after))
        db = SessionLocal()
        try:
            stored_after = db.get(QuizQuestion, qid)
            text_after = stored_after.question if stored_after is not None else None
            opts_after = stored_after.get_options() if stored_after is not None else None
        finally:
            db.close()
        check("quiz-import dry_run: stored question text unchanged",
              text_after == text_before and "DRYRUN-MARKER" not in (text_after or ""),
              text_after)
        check("quiz-import dry_run: stored options unchanged", opts_after == opts_before,
              opts_after)

        r = c.post("/api/admin/quests/quiz-import-default", json={"dry_run": True})
        check("quiz-import-default dry_run: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        rep = r.json() if r.status_code == 200 else {}
        check("quiz-import-default dry_run reports translation coverage",
              rep.get("i18n_coverage") == {"ru": len(bank), "zh-HK": len(bank)}, rep.get("i18n_coverage"))
        check("quiz-import-default dry_run prints coverage on one line",
              f"ru: {len(bank)}" in (rep.get("i18n_line") or ""), rep.get("i18n_line"))

        # ── a translated bank is refused when a translation is short ────
        tr_bank = qquiz.load_default_bank()
        tr_bank[0]["i18n"]["ru"]["o"] = tr_bank[0]["i18n"]["ru"]["o"][:3]
        r = c.post("/api/admin/quests/quiz-import", json={"bank": tr_bank, "dry_run": True})
        check("a translation with 3 options: 422", r.status_code == 422, f"{r.status_code}")
        det = (r.json() or {}).get("detail") if r.status_code == 422 else {}
        check("422 names the language and the field",
              isinstance(det, dict)
              and "i18n.ru.o" in {e["field"] for e in det.get("errors", [])},
              str(det)[:300])

        tr_bank = qquiz.load_default_bank()
        tr_bank[0]["i18n"]["de"] = dict(tr_bank[0]["i18n"]["ru"])
        r = c.post("/api/admin/quests/quiz-import", json={"bank": tr_bank, "dry_run": True})
        check("an unsupported language: 422", r.status_code == 422, f"{r.status_code}")
        det = (r.json() or {}).get("detail") if r.status_code == 422 else {}
        check("422 names the unsupported language",
              isinstance(det, dict)
              and "i18n.de" in {e["field"] for e in det.get("errors", [])},
              str(det)[:300])

        # ── the quiz itself answers in the caller's language ────────────
        # Everything here goes through the routes: the language is the one on
        # the account (users.lang beats the op_lang cookie — app/i18n.py
        # negotiate), set exactly the way the switcher sets it.
        def _fresh_quiz_q():
            db = SessionLocal()
            try:
                db.query(QuizAttempt).delete()
                db.commit()
            finally:
                db.close()
            r = c.post("/api/quests/quiz/start")
            return ((r.json().get("question") or {}).get("q")
                    if r.status_code == 200 else f"HTTP {r.status_code}")

        def _cyr(t):
            return any("\u0430" <= ch.lower() <= "\u044f" for ch in (t or ""))

        # A user who never chose a language, with no op_lang cookie, gets the
        # default — English; an op_lang=ru cookie alone is enough for Russian.
        db = SessionLocal()
        try:
            me = db.query(User).filter(User.person_id == "admin1").one()
            me.lang = None
            db.commit()
        finally:
            db.close()
        c.cookies.delete("op_lang")
        q_default = _fresh_quiz_q()
        check("quiz/start: the default user is served English",
              bool(q_default) and not q_default.startswith("HTTP") and not _cyr(q_default),
              q_default)
        c.cookies.set("op_lang", "ru")
        q_cookie_ru = _fresh_quiz_q()
        check("quiz/start: an op_lang=ru cookie is served Cyrillic",
              _cyr(q_cookie_ru), q_cookie_ru)
        c.cookies.delete("op_lang")

        served = {}
        for lang in ("ru", "en", "zh-HK"):
            c.put("/api/me/lang", json={"lang": lang})
            db = SessionLocal()
            try:
                db.query(QuizAttempt).delete()
                db.commit()
            finally:
                db.close()
            r = c.post("/api/quests/quiz/start")
            served[lang] = ((r.json().get("question") or {}).get("q")
                            if r.status_code == 200 else f"HTTP {r.status_code}")
        c.put("/api/me/lang", json={"lang": "en"})       # back to the default
        check("quiz/start serves three different texts for three languages",
              len(set(served.values())) == 3, served)
        check("quiz/start ru (users.lang) is the Russian translation",
              any("\u0430" <= ch <= "\u044f" for ch in (served["ru"] or "")), served["ru"])
        check("quiz/start en is the English source, no Cyrillic",
              not any("\u0430" <= ch.lower() <= "\u044f" for ch in (served["en"] or "")),
              served["en"])
        check("quiz/start zh-HK carries Han characters",
              any("\u4e00" <= ch <= "\u9fff" for ch in (served["zh-HK"] or "")),
              served["zh-HK"])

        # ── import validation: the 422 body is the WHOLE report ──────────
        bad_bank = qquiz.load_default_bank()
        bad_bank[0]["q"] = "q" * (qquiz.MAX_Q + 1)
        bad_bank[1]["o"][1] = bad_bank[1]["o"][0].upper()
        r = c.post("/api/admin/quests/quiz-import", json={"bank": bad_bank, "dry_run": True})
        check("quiz-import over-long question: 422", r.status_code == 422, f"{r.status_code}")
        detail = (r.json() or {}).get("detail") if r.status_code == 422 else {}
        check("422 detail is the full report (errors/count/days)",
              isinstance(detail, dict) and {"errors", "count", "days"} <= set(detail),
              str(detail)[:300])
        check("422 detail flags both the long question and the duplicate options",
              isinstance(detail, dict)
              and {"q", "o"} <= {e["field"] for e in detail.get("errors", [])},
              str(detail)[:300])

        # ── XSS / render (template may not exist yet — phase 6 in flight) ──
        if not Path("templates/pages/quests.html").exists():
            print("SKIP  /quests HTML render check: templates/pages/quests.html not present yet")
        else:
            r = c.get("/quests")
            check("/quests render: 200", r.status_code == 200, f"{r.status_code}")
            if r.status_code == 200 and "data-state=" in r.text:
                # Jinja autoescape must turn any literal '<' inside the JSON
                # blob into '&lt;' when it lands inside an HTML attribute —
                # a raw '<' there would mean the template interpolated
                # state_json unescaped (|safe or similar), i.e. an XSS hole.
                attr_start = r.text.index("data-state=") + len("data-state=")
                quote = r.text[attr_start]
                attr_end = r.text.index(quote, attr_start + 1)
                attr_value = r.text[attr_start + 1:attr_end]
                check("/quests render: data-state attribute has no raw '<'", "<" not in attr_value, attr_value[:200])
            else:
                check("/quests render: data-state attribute present", False, r.text[:200])

        # ── Weekend / holiday: no daily chest, no quiz, badge silent ──
        saved_today = qsvc._TODAY_OVERRIDE
        qsvc._TODAY_OVERRIDE = date(2026, 9, 5)  # Saturday
        try:
            login(c, "admin1")
            c.post("/api/admin/users", json={"person_id": "wknd1", "role": "visitor", "display_name": "Wk End"})
            login(c, "wknd1")
            r = c.post("/api/quests/open")
            check("weekend: open → 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
            check("weekend: 409 names the weekend", "day off" in r.text, r.text[:120])
            r = c.get("/api/quests/badge")
            check("weekend: badge open=true (nothing to show)", r.status_code == 200 and r.json()["open"] is True, r.text[:120])
            r = c.get("/api/quests/state")
            st = r.json()
            check("weekend: state.working_today false", st.get("working_today") is False)
            check("weekend: state.next_working = Monday", st.get("next_working") == "2026-09-07", st.get("next_working"))
            check("weekend: opened_today false", st.get("opened_today") is False)
            check("weekend: path starts Monday, not flagged today",
                  st["path"][0]["day"] == "2026-09-07" and st["path"][0]["today"] is False and st["path"][0]["done"] is False,
                  st["path"][0])
            check("weekend: quiz unavailable", st["quiz"]["available"] is False and st["quiz"]["working_today"] is False, st["quiz"])
            r = c.post("/api/quests/quiz/start")
            check("weekend: quiz start → 409", r.status_code == 409 and "day off" in r.text, f"{r.status_code} {r.text[:120]}")
            db = SessionLocal()
            try:
                me = db.query(User).filter(User.person_id == "wknd1").first()
                n = db.query(QuestOpening).filter(QuestOpening.user_id == me.id).count()
            finally:
                db.close()
            check("weekend: no opening rows written", n == 0, n)
        finally:
            qsvc._TODAY_OVERRIDE = saved_today

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
