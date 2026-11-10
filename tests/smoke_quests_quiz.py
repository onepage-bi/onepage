"""Smoke test for the Quests quiz engine (app/services/quests_quiz.py).

Boot idiom from the onepage-testing skill (tests/smoke_quality.py lines
20-60): TestClient only to run the lifespan (tables + initial admin), then
the engine is called directly through SessionLocal — it is a service
module, the routes land in a later phase.
"""
import copy
import json
import os
import sys
import tempfile
from datetime import timedelta

TMP = tempfile.mkdtemp(prefix="op-quiz-smoke-")
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
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import quests as qsvc  # noqa: E402

# The daily surprise and the quiz run on working days only: on a weekend the
# test looks at the next working day, otherwise it would fail every Saturday.
if not qsvc.is_working(qsvc.today()):
    qsvc._TODAY_OVERRIDE = qsvc.next_working(qsvc.today())
from app.database import SessionLocal  # noqa: E402
from app.models import AuditLog, QuestProfile, QuizAttempt, QuizQuestion, User  # noqa: E402
from app import i18n  # noqa: E402
from app.services import quests, quests_quiz as Q  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return False


def get_profile(db, user_id):
    p = db.get(QuestProfile, user_id)
    if p is None:
        p = QuestProfile(user_id=user_id, points=0, streak=3, quiz_correct_total=0)
        db.add(p)
        db.flush()
    return p


def reset_attempts(db):
    db.query(QuizAttempt).delete()
    db.commit()
    Q._opp_cache.clear()


def main():
    with TestClient(app) as c:
        c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
        r = c.post("/api/admin/users", json={"person_id": "quiz1", "role": "visitor",
                                             "display_name": "Quiz One"})
        check("test user created", r.status_code == 200 and r.json().get("ok") is True,
              f"{r.status_code} {r.text[:200]}")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.person_id == "quiz1").first()
        check("user loaded", user is not None)
        profile = get_profile(db, user.id)
        db.commit()

        bank = Q.load_default_bank()
        # The bank size is read off the bank itself: when it grows the test needs no edit.
        N = len(bank)
        DAYS = Q.bank_days_of(bank)
        check("bank size is a whole number of days", N == DAYS * Q.PER_DAY and DAYS >= Q.BANK_DAYS_MIN, (N, DAYS))

        # ── import_bank: dry-run ─────────────────────────────────────
        res = Q.import_bank(db, bank, dry_run=True)
        check("dry_run ok", res["ok"] is True, str(res["errors"])[:300])
        check("dry_run count = bank size", res["count"] == N, res["count"])
        check("dry_run days = bank days", res["days"] == DAYS, res["days"])
        check("dry_run flag", res["dry_run"] is True)
        check("dry_run wrote nothing",
              (db.query(QuizQuestion).count()) == 0, db.query(QuizQuestion).count())
        check("dry_run reports translation coverage",
              res["i18n_coverage"] == {"ru": N, "zh-HK": N}, res.get("i18n_coverage"))
        check("dry_run coverage has a one-line form",
              f"ru: {N}" in (res.get("i18n_line") or ""), res.get("i18n_line"))

        # ── import_bank: malformed ───────────────────────────────────
        def bad(mutate):
            b = copy.deepcopy(bank)
            mutate(b)
            return Q.import_bank(db, b, dry_run=True)

        def fields(r):
            return {e["field"] for e in r["errors"]}

        r1 = bad(lambda b: b.pop(0))                      # N-1 items + day 1 has 2
        check("one item short -> not ok", r1["ok"] is False)
        check("one item short -> bank+day errors", {"bank", "day"} <= fields(r1), fields(r1))
        check("one item short -> count 0", r1["count"] == 0)

        def _a4(b):
            b[0]["a"] = 4
        r2 = bad(_a4)
        check("a=4 -> error on 'a'", r2["ok"] is False and "a" in fields(r2), fields(r2))

        def _three_opts(b):
            b[1]["o"] = b[1]["o"][:3]
        r3 = bad(_three_opts)
        check("3 options -> error on 'o'", r3["ok"] is False and "o" in fields(r3), fields(r3))

        def _sec10(b):
            b[2]["sec"] = 10
        r4 = bad(_sec10)
        check("sec=10 -> error on 'sec'", r4["ok"] is False and "sec" in fields(r4), fields(r4))

        def _dup(b):
            b[1]["id"] = b[0]["id"]
        r5 = bad(_dup)
        check("duplicate id -> error on 'id'", r5["ok"] is False and "id" in fields(r5), fields(r5))
        check("duplicate id reason is english by default",
              any("duplicate id" in e["reason"] for e in r5["errors"]), str(r5["errors"])[:200])
        _tok = i18n.set_lang("ru")
        try:
            r5ru = bad(_dup)
        finally:
            i18n.reset(_tok)
        check("duplicate id reason is russian for a russian admin",
              # Cyrillic on purpose: checks the Russian language pack.
              any("дубл" in e["reason"] for e in r5ru["errors"]), str(r5ru["errors"])[:200])

        def _day99(b):
            b[3]["day"] = 99
        r6 = bad(_day99)
        check("day=99 -> error on 'day'", r6["ok"] is False and "day" in fields(r6), fields(r6))

        def _longq(b):
            b[0]["q"] = "x" * (Q.MAX_Q + 1)
        r7 = bad(_longq)
        check("q longer than the cap -> error on 'q'",
              r7["ok"] is False and "q" in fields(r7), fields(r7))

        def _longopt(b):
            b[0]["o"][1] = "x" * (Q.MAX_OPT + 1)
        r8 = bad(_longopt)
        check("option longer than the cap -> error on 'o'",
              r8["ok"] is False and "o" in fields(r8), fields(r8))

        def _dupopt(b):
            b[0]["o"][1] = "  " + b[0]["o"][0].upper() + " "
        r9 = bad(_dupopt)
        check("options equal ignoring case/space -> error on 'o'",
              r9["ok"] is False and "o" in fields(r9), fields(r9))

        def _longe(b):
            b[0]["e"] = "x" * (Q.MAX_EXPL + 1)
        r10 = bad(_longe)
        check("explanation longer than the cap -> error on 'e'",
              r10["ok"] is False and "e" in fields(r10), fields(r10))

        def _id0(b):
            b[0]["id"] = 0
        r11 = bad(_id0)
        check("id=0 -> error on 'id'", r11["ok"] is False and "id" in fields(r11), fields(r11))

        def _idbig(b):
            b[0]["id"] = Q.MAX_QID + 1
        r12 = bad(_idbig)
        check("id above the cap -> error on 'id'",
              r12["ok"] is False and "id" in fields(r12), fields(r12))

        def _nan(b):
            b[0]["sec"] = float("nan")
        r13 = bad(_nan)
        check("sec=NaN -> error on 'sec'",
              r13["ok"] is False and "sec" in fields(r13), fields(r13))
        check("NaN reason says «finite»",
              any("must be finite" in e["reason"] for e in r13["errors"]), str(r13["errors"])[:200])

        def _longt(b):
            b[0]["t"] = "x" * (Q.MAX_TOPIC + 1)
        r14 = bad(_longt)
        check("topic longer than the cap -> error on 't'",
              r14["ok"] is False and "t" in fields(r14), fields(r14))

        # ── import_bank: translations ────────────────────────────────
        def _tr_three_opts(b):
            b[0]["i18n"]["ru"]["o"] = b[0]["i18n"]["ru"]["o"][:3]
        r15 = bad(_tr_three_opts)
        check("translation with 3 options -> error on 'i18n.ru.o'",
              r15["ok"] is False and "i18n.ru.o" in fields(r15), fields(r15))

        def _tr_empty_q(b):
            b[0]["i18n"]["ru"]["q"] = "   "
        r16 = bad(_tr_empty_q)
        check("empty translated question -> error on 'i18n.ru.q'",
              r16["ok"] is False and "i18n.ru.q" in fields(r16), fields(r16))

        def _tr_empty_e(b):
            b[0]["i18n"]["zh-HK"]["e"] = ""
        r17 = bad(_tr_empty_e)
        check("empty translated explanation -> error on 'i18n.zh-HK.e'",
              r17["ok"] is False and "i18n.zh-HK.e" in fields(r17), fields(r17))

        def _tr_unknown_lang(b):
            b[0]["i18n"]["de"] = dict(b[0]["i18n"]["ru"])
        r18 = bad(_tr_unknown_lang)
        check("unsupported language -> error on 'i18n.de'",
              r18["ok"] is False and "i18n.de" in fields(r18), fields(r18))

        def _tr_en(b):
            b[0]["i18n"]["en"] = {"t": "x", "q": "x", "o": list(b[0]["o"]), "e": "x"}
        r19 = bad(_tr_en)
        check("an 'en' block is refused: the question itself is the English",
              r19["ok"] is False and "i18n.en" in fields(r19), fields(r19))

        def _tr_not_object(b):
            b[0]["i18n"] = ["ru"]
        r20 = bad(_tr_not_object)
        check("i18n that is not an object -> error on 'i18n'",
              r20["ok"] is False and "i18n" in fields(r20), fields(r20))

        def _no_i18n(b):
            for q in b:
                q.pop("i18n", None)
        r21 = bad(_no_i18n)
        check("a bank with no translations at all is still valid",
              r21["ok"] is True, str(r21["errors"])[:200])
        check("…and its coverage is zero",
              r21["i18n_coverage"] == {"ru": 0, "zh-HK": 0}, r21.get("i18n_coverage"))

        # ── import_bank: real write ──────────────────────────────────
        res = Q.import_bank(db, bank, dry_run=False)
        db.commit()
        check("real import ok", res["ok"] is True and res["count"] == N, str(res)[:200])
        active = db.query(QuizQuestion).filter(QuizQuestion.active.is_(True)).count()
        check("all bank questions active", active == N, active)

        # ── the language of a served question ────────────────────────
        # `public_question` reads i18n.current_lang(), so the language is set
        # the way a request sets it, not passed in.
        # Take the bank's FIRST question, not id == 1: the numbering belongs to
        # the content, not to the test. When the old bank was archived the new
        # questions started at 46 and a hard-coded 1 broke everything at once.
        src = bank[0]
        q1 = db.query(QuizQuestion).filter(QuizQuestion.id == src["id"]).first()
        for lang, want_q, want_o in (
            ("en", src["q"], src["o"][0]),
            ("ru", src["i18n"]["ru"]["q"], src["i18n"]["ru"]["o"][0]),
            ("zh-HK", src["i18n"]["zh-HK"]["q"], src["i18n"]["zh-HK"]["o"][0]),
        ):
            token = i18n.set_lang(lang)
            try:
                pub = Q.public_question(q1, 0)
                check(f"question text is served in {lang}", pub["q"] == want_q,
                      pub["q"][:60])
                check(f"options are served in {lang}", pub["o"][0] == want_o,
                      pub["o"][0][:60])
                check(f"the explanation is served in {lang}",
                      q1.localized(lang)["e"] ==
                      (src["e"] if lang == "en" else src["i18n"][lang]["e"]),
                      q1.localized(lang)["e"][:60])
            finally:
                i18n.reset(token)

        _cyr = lambda t: any("\u0400" <= ch <= "\u04ff" for ch in t)  # noqa: E731
        check("the default question is English, its ru block is Russian",
              not _cyr(src["q"]) and _cyr(src["i18n"]["ru"]["q"]), src["q"][:60])
        check("every option keeps its place, so answer_idx is language-free",
              all(len(q1.localized(l)["o"]) == len(q1.get_options())
                  for l in ("ru", "en", "zh-HK")))

        # A question with no translation stays English in every language.
        q1.set_i18n(None)
        db.commit()
        for lang in ("ru", "en", "zh-HK"):
            token = i18n.set_lang(lang)
            try:
                check(f"an untranslated question falls back to English ({lang})",
                      Q.public_question(q1, 0)["q"] == src["q"])
            finally:
                i18n.reset(token)
        q1.set_i18n(src["i18n"])     # put it back for the checks below
        db.commit()
        # `seed_default_bank` below wipes the table; a q1 still in the identity
        # map would be refreshed out of rows that no longer exist.
        db.expunge(q1)

        st = Q.bank_status(db)
        check("bank_status active = bank size", st["active"] == N, st)
        check("bank_status days = bank days", st["days"] == DAYS, st)
        check("bank_status days_complete", st["days_complete"] is True, st)
        check("bank_status last_imported_at", bool(st["last_imported_at"]), st)

        # re-import: previous rows go inactive, then active again — still the whole bank
        res = Q.import_bank(db, bank, dry_run=False)
        db.commit()
        check("re-import ok", res["ok"] is True and res["count"] == N)
        check("re-import keeps the whole bank active",
              db.query(QuizQuestion).filter(QuizQuestion.active.is_(True)).count() == N)
        check("re-import creates no duplicates", db.query(QuizQuestion).count() == N,
              db.query(QuizQuestion).count())

        # ── seed_default_bank idempotent ─────────────────────────────
        n = Q.seed_default_bank(db)
        check("seed on a full bank -> 0", n == 0, n)
        db.query(QuizQuestion).delete()
        db.commit()
        n = Q.seed_default_bank(db)
        db.commit()
        check("seed on empty bank -> whole bank", n == N, n)
        check("seed twice -> 0", Q.seed_default_bank(db) == 0)

        # ── start ────────────────────────────────────────────────────
        reset_attempts(db)
        s = Q.start(db, profile)
        db.commit()
        check("start total 3", s["total"] == 3, s["total"])
        check("start does not stamp served_at", s["served"] is False, s.get("served"))
        check("start remaining_sec None until begin", s["remaining_sec"] is None,
              s.get("remaining_sec"))
        att0 = db.query(QuizAttempt).filter(QuizAttempt.user_id == user.id).first()
        check("start wrote no served_at into answers_json",
              "served_at" not in att0.get_answers()[0], att0.answers_json)
        check("start index 0", s["index"] == 0)
        check("start has token", bool(s["token"]))
        check("start bank_day matches", s["day"] == Q.day_of(db, quests.today()))
        check("start question has 4 options", len(s["question"]["o"]) == 4)
        check("question keys are public",
              set(s["question"]) == {"index", "id", "topic", "q", "o", "sec"}, set(s["question"]))
        dump = json.dumps(s, ensure_ascii=False, default=str)
        check("payload has no answer_idx/explanation keys",
              # As JSON KEYS: an English question may say the word "explanation".
              '"answer_idx":' not in dump and '"explanation":' not in dump)
        qrow = db.get(QuizQuestion, s["question"]["id"])
        check("payload does not leak the explanation text",
              qrow.explanation and qrow.explanation not in dump)

        # resume BEFORE begin(): the question is not served yet
        att = db.query(QuizAttempt).filter(QuizAttempt.user_id == user.id).first()
        s_pre = Q.start(db, profile)
        db.commit()
        check("resume before begin: served False", s_pre["served"] is False, s_pre)

        # ── begin: stamps once, then reports the true remainder ──────
        b1 = Q.begin(db, profile, s["token"])
        db.commit()
        db.refresh(att)
        served_before = att.get_answers()[0]["served_at"]
        q0row = db.get(QuizQuestion, s["question"]["id"])
        check("begin stamps served_at", bool(served_before), att.answers_json)
        check("begin returns the question", b1["question"]["id"] == s["question"]["id"])
        check("begin index 0", b1["index"] == 0)
        check("begin remaining_sec = sec", b1["remaining_sec"] == q0row.sec,
              (b1["remaining_sec"], q0row.sec))
        check("begin has score/opponent", "score" in b1 and "opponent" in b1, sorted(b1))
        b2 = Q.begin(db, profile, s["token"])
        db.commit()
        db.refresh(att)
        check("begin is idempotent: served_at untouched",
              att.get_answers()[0]["served_at"] == served_before,
              (att.get_answers()[0].get("served_at"), served_before))
        check("begin twice reports the same served_at", b2["served_at"] == served_before,
              (b2["served_at"], served_before))

        # resume AFTER begin: same token/index, served_at untouched
        s2 = Q.start(db, profile)
        db.commit()
        db.refresh(att)
        check("resume same token", s2["token"] == s["token"])
        check("resume same index", s2["index"] == 0)
        check("resume served True", s2["served"] is True, s2)
        check("resume remaining_sec is an int <= sec",
              isinstance(s2["remaining_sec"], int) and 0 <= s2["remaining_sec"] <= q0row.sec,
              s2["remaining_sec"])
        check("resume does not reset served_at",
              att.get_answers()[0]["served_at"] == served_before)

        # ── answer: scoring windows ──────────────────────────────────
        def set_served(attempt, idx, seconds_ago):
            answers = attempt.get_answers()
            answers[idx]["served_at"] = Q._iso(Q._utcnow() - timedelta(seconds=seconds_ago))
            attempt.set_answers(answers)
            db.flush()
            db.commit()

        q1 = db.get(QuizQuestion, att.get_q_ids()[0])
        set_served(att, 0, q1.sec / 2 + 1)          # slow but in time
        a1 = Q.answer(db, profile, s["token"], q1.answer_idx, index=0)
        db.commit()
        check("correct (slow) -> 10 pts", a1["correct"] is True and a1["points"] == 10, a1["points"])
        check("answer reveals answer_idx", a1["answer_idx"] == q1.answer_idx)
        check("answer reveals explanation", a1["explanation"] == q1.explanation)
        check("answer has next question", a1["next"] and a1["next"]["index"] == 1)
        # 2026-09-10: the next question's clock must NOT start with the
        # verdict — the explanation is read first. begin() starts it later.
        check("next does not carry served_at", not a1["next"].get("served_at"), a1["next"])
        check("next is reported as not served", a1["next"].get("served") is False, a1["next"])
        check("next remaining_sec == next sec",
              a1["next"]["remaining_sec"] == a1["next"]["sec"], a1["next"])
        db.refresh(att)
        check("answers_json: next entry has no served_at",
              "served_at" not in att.get_answers()[1], att.answers_json)
        # a long read of the explanation costs nothing: begin() for q2 gives
        # the full window, and an answer right after it is scored
        b_q2 = Q.begin(db, profile, s["token"])
        db.commit()
        check("begin for q2 picks index 1", b_q2["index"] == 1, b_q2["index"])
        db.refresh(att)
        q2row = db.get(QuizQuestion, att.get_q_ids()[1])
        check("begin for q2 gives the full window", b_q2["remaining_sec"] == q2row.sec,
              (b_q2["remaining_sec"], q2row.sec))
        check("answer not done", a1["done"] is False)
        check("score me=1", a1["score"]["me"] == 1, a1["score"])

        # stale: repeat with the same index
        st_res = raises(Q.QuizStale, Q.answer, db, profile, s["token"], q1.answer_idx, index=0)
        db.rollback()
        check("index mismatch -> QuizStale", st_res is True, st_res)

        # wrong answer on q2
        db.refresh(att)
        q2 = db.get(QuizQuestion, att.get_q_ids()[1])
        wrong = (q2.answer_idx + 1) % 4
        a2 = Q.answer(db, profile, s["token"], wrong, index=1)
        db.commit()
        check("wrong -> 0 pts", a2["correct"] is False and a2["points"] == 0, a2["points"])

        # unknown token
        nf = raises(Q.QuizNotFound, Q.answer, db, profile, "deadbeef", 0)
        db.rollback()
        check("unknown token -> QuizNotFound", nf is True, nf)

        # third answer, fast → 15, finishes the attempt
        db.refresh(att)
        q3 = db.get(QuizQuestion, att.get_q_ids()[2])
        set_served(att, 2, max(1, q3.sec / 2 - 1))
        pts_before, corr_before = profile.points, profile.quiz_correct_total
        a3 = Q.answer(db, profile, s["token"], q3.answer_idx, index=2)
        db.commit()
        check("fast correct -> 15 pts", a3["correct"] is True and a3["points"] == 15, a3["points"])
        check("third answer done", a3["done"] is True)
        check("summary correct 2", a3["summary"]["correct"] == 2, a3["summary"])
        check("summary points 25", a3["summary"]["points"] == 25, a3["summary"])
        check("summary has bot", isinstance(a3["summary"]["bot"], int))
        db.refresh(profile)
        check("profile.points += 25", profile.points == pts_before + 25,
              f"{pts_before} -> {profile.points}")
        check("profile.quiz_correct_total += 2", profile.quiz_correct_total == corr_before + 2,
              f"{corr_before} -> {profile.quiz_correct_total}")
        db.refresh(att)
        check("attempt finished", att.finished_at is not None)
        check("attempt.correct 2 / points 25", att.correct == 2 and att.points == 25,
              f"{att.correct}/{att.points}")

        has_grant = hasattr(quests, "grant_opening")
        if has_grant:
            check("opening granted with source quiz",
                  a3["summary"]["opening"] is not None
                  and a3["summary"]["opening"].get("source") == "quiz",
                  str(a3["summary"]["opening"]))
        else:
            check("no grant_opening yet -> opening None, attempt still finished",
                  a3["summary"]["opening"] is None)

        row = db.query(AuditLog).filter(AuditLog.action == "quests_quiz_finish").first()
        check("audit quests_quiz_finish written", row is not None)
        if row is not None:
            d = row.get_detail()
            check("audit detail carries score", d.get("correct") == 2 and d.get("points") == 25
                  and "bank_day" in d, str(d))
            check("audit actor is the player", row.actor_id == user.id)

        # already played
        ap = raises(Q.AlreadyPlayed, Q.start, db, profile)
        db.rollback()
        check("start after finish -> AlreadyPlayed", ap is True, ap)

        stt = Q.status(db, profile)
        check("status played_today", stt["played_today"] is True, stt)
        check("status last", stt["last"] == {"correct": 2, "points": 25}, stt["last"])
        check("status available", stt["available"] is True)
        check("status level", stt["level"] == quests.level(profile.quiz_correct_total), stt)

        # ── timeout ──────────────────────────────────────────────────
        reset_attempts(db)
        s = Q.start(db, profile)
        db.commit()
        att = db.query(QuizAttempt).filter(QuizAttempt.user_id == user.id).first()
        q1 = db.get(QuizQuestion, att.get_q_ids()[0])
        set_served(att, 0, q1.sec + 5)
        a = Q.answer(db, profile, s["token"], q1.answer_idx, index=0)
        db.commit()
        check("timeout -> correct False even with the right pick",
              a["correct"] is False and a["points"] == 0, a)

        # pick None (client-side timeout)
        db.refresh(att)
        a = Q.answer(db, profile, s["token"], None, index=1)
        db.commit()
        check("pick None -> not correct", a["correct"] is False and a["points"] == 0, a)

        # ── resume after expiry: auto-timeout, then the next question ─
        reset_attempts(db)
        s = Q.start(db, profile)
        Q.begin(db, profile, s["token"])
        db.commit()
        att = db.query(QuizAttempt).filter(QuizAttempt.user_id == user.id).first()
        qa = db.get(QuizQuestion, att.get_q_ids()[0])
        set_served(att, 0, qa.sec + Q.GRACE_SEC + 5)
        s_res = Q.start(db, profile)
        db.commit()
        db.refresh(att)
        check("resume after expiry moves to index 1", s_res.get("index") == 1, s_res)
        check("resume after expiry: next question not served yet",
              s_res.get("served") is False and s_res.get("remaining_sec") is None, s_res)
        ans0 = att.get_answers()[0]
        check("expired question recorded as a timeout",
              ans0.get("pick") is None and ans0.get("correct") is False
              and ans0.get("pts") == 0 and ans0.get("timeout") is True, ans0)
        check("auto-timeout scored nothing", s_res["score"]["me"] == 0, s_res["score"])

        # ...and if the auto-timeout eats the LAST question, start() finishes
        answers = att.get_answers()
        while len(answers) < 3:
            answers.append({"qid": att.get_q_ids()[len(answers)]})
        for i in (1, 2):
            answers[i]["served_at"] = Q._iso(Q._utcnow() - timedelta(
                seconds=db.get(QuizQuestion, att.get_q_ids()[i]).sec + Q.GRACE_SEC + 5))
        att.set_answers(answers)
        db.commit()
        s_done = Q.start(db, profile)
        db.commit()
        check("auto-timeout of the last question finishes the attempt",
              s_done.get("done") is True and isinstance(s_done.get("summary"), dict), s_done)
        check("auto-timeout summary scores zero",
              s_done["summary"]["correct"] == 0 and s_done["summary"]["points"] == 0,
              s_done["summary"])
        db.refresh(att)
        check("auto-timeout wrote finished_at", att.finished_at is not None)
        ap2 = raises(Q.AlreadyPlayed, Q.start, db, profile)
        db.rollback()
        check("start after an auto-timeout finish -> AlreadyPlayed", ap2 is True, ap2)

        # begin() on a finished attempt is AlreadyPlayed, unknown token is not found
        bf = raises(Q.AlreadyPlayed, Q.begin, db, profile, s_done.get("token") or "x")
        db.rollback()
        bn = raises(Q.QuizNotFound, Q.begin, db, profile, "deadbeef")
        db.rollback()
        check("begin on unknown token -> QuizNotFound", bn is True, bn)
        check("begin on a finished attempt -> QuizNotFound/AlreadyPlayed",
              bf is True or bn is True, bf)

        # ── opponent ─────────────────────────────────────────────────
        reset_attempts(db)
        Q._opp_cache.clear()
        d = quests.today()
        check("opponent fallback 2", Q.opponent(db, d) == 2, Q.opponent(db, d))

        Q._opp_cache.clear()
        prev = quests.prev_working(d)
        other = db.query(User).filter(User.person_id == "admin1").first()
        for uid, corr in ((user.id, 3), (other.id, 1)):
            at = QuizAttempt(user_id=uid, day=prev, bank_day=quests.quiz_bank_day(prev),
                             token=f"t{uid}", started_at=Q._utcnow(), finished_at=Q._utcnow(),
                             q_ids_json="[]", answers_json="[]", correct=corr, points=corr * 10)
            db.add(at)
        db.commit()
        Q._opp_cache.clear()
        check("opponent avg(3,1) -> 2", Q.opponent(db, d) == 2, Q.opponent(db, d))

        # cache: a changed result does not move the value until the TTL expires
        db.query(QuizAttempt).filter(QuizAttempt.user_id == other.id,
                                     QuizAttempt.day == prev).update({QuizAttempt.correct: 3})
        db.commit()
        check("cached opponent unchanged", Q.opponent(db, d) == 2, Q.opponent(db, d))
        real_time = Q.time.time
        Q.time.time = lambda: real_time() + Q._OPP_TTL + 1  # type: ignore[assignment]
        try:
            check("opponent recomputed after TTL", Q.opponent(db, d) == 3, Q.opponent(db, d))
        finally:
            Q.time.time = real_time  # type: ignore[assignment]

        # ── QuizUnavailable ──────────────────────────────────────────
        reset_attempts(db)
        db.query(QuizQuestion).update({QuizQuestion.active: False}, synchronize_session=False)
        db.commit()
        un = raises(Q.QuizUnavailable, Q.start, db, profile)
        db.rollback()
        check("empty bank -> QuizUnavailable", un is True, un)
        check("status available False", Q.status(db, profile)["available"] is False)
        check("bank_status days_complete False", Q.bank_status(db)["days_complete"] is False)
    finally:
        db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
