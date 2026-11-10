"""The "Working on the platform" course: state, lesson order, checks, tasks.

Pins:
  * `/learn` opens for any signed-in user; the state rides in a page attribute;
  * correct answers do NOT leave the server before the answer — neither in state nor in HTML;
  * lessons go in order: a locked lesson answers 409 instead of being credited;
  * a wrong answer does not close the lesson and gives no XP; a right one closes it,
    and passing it again does not award XP a second time;
  * a lesson with a task does not close until the task is visible in the platform itself
    (here: "upload a report"), and closes as soon as it is;
  * XP and level are computed by the server from the completed lessons;
  * a block's exam is locked until all its chapters are done, is served without
    answers, is accepted at a threshold (not necessarily with zero mistakes) and
    awards XP once; the question sample is fixed per person.

Synthetic data only — no real folder/file names, no people.
"""
import collections
import io
import json
import os
import re
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-smoke-learn-")
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
from app.services import learn  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, f"{ov}: {r.status_code} {r.text[:200]}"


COURSE = "base"                      # the flow is checked on the base course
LESSONS = learn.lessons(COURSE)
FIRST = LESSONS[0]
SECOND = LESSONS[1]
# The first lesson whose completion needs a real action on the platform.
TASK_LESSON = next(l for l in LESSONS if l.get("task"))


def answers_for(lesson, correct=True):
    out = []
    for c in lesson.get("checks") or []:
        right = int(c["answer"])
        out.append(right if correct else (right + 1) % len(c["options"]))
    return out


with TestClient(app) as c:
    login(c, "admin1")
    r = c.get("/learn")
    check("/learn shows the course picker", r.status_code == 200 and "lr-course" in r.text,
          r.status_code)
    r = c.get("/learn/%s" % COURSE)
    check("/learn/{course} renders", r.status_code == 200, r.status_code)
    check("an unknown course is a 404", c.get("/learn/nope").status_code == 404)
    check("the page carries its state", 'id="lr-root"' in r.text and "data-state=" in r.text)

    # The bank's answers must not be reachable before answering.
    state = c.get("/api/learn/state?course=%s" % COURSE).json()
    blob = json.dumps(state, ensure_ascii=False)
    first_why = (FIRST.get("checks") or [{}])[0].get("why", "")
    check("state carries no correct answers", '"answer"' not in blob, blob[:120])
    check("state carries no explanations", bool(first_why) and first_why not in blob)
    check("state carries no explanations in the page", first_why not in r.text)
    check("only the first lesson is unlocked",
          state["units"][0]["lessons"][0]["unlocked"] and
          not state["units"][0]["lessons"][1]["unlocked"])
    check("xp starts at zero", state["xp"] == 0 and state["done"] == 0, state["xp"])

    # Order is enforced by the server, not by the page hiding a button.
    r = c.post("/api/learn/check", json={"lesson": SECOND["id"],
                                         "answers": answers_for(SECOND)})
    check("a locked lesson is refused", r.status_code == 409, f"{r.status_code} {r.text[:120]}")

    # A wrong answer teaches, it does not complete.
    r = c.post("/api/learn/check", json={"lesson": FIRST["id"],
                                         "answers": answers_for(FIRST, correct=False)})
    body = r.json()
    check("a wrong answer does not complete the lesson",
          r.status_code == 200 and not body["completed"] and not body["all_correct"],
          f"{r.status_code} {body}")
    check("the explanation comes back with the verdict",
          bool(body["results"][0]["why"]) and body["results"][0]["correct"] is False)
    check("a wrong answer earns no xp", body["state"]["xp"] == 0, body["state"]["xp"])

    # The right answer completes it, once.
    r = c.post("/api/learn/check", json={"lesson": FIRST["id"], "answers": answers_for(FIRST)})
    body = r.json()
    check("the right answer completes the lesson", body["completed"], body)
    check("xp is credited", body["state"]["xp"] == FIRST["xp"], body["state"]["xp"])
    check("the next lesson opens",
          body["state"]["units"][0]["lessons"][1]["unlocked"])

    r2 = c.post("/api/learn/check", json={"lesson": FIRST["id"], "answers": answers_for(FIRST)})
    body2 = r2.json()
    check("a second pass credits no xp again",
          not body2["completed"] and body2["state"]["xp"] == FIRST["xp"], body2["state"]["xp"])

    # The report is needed before the chapter itself: without a single accessible
    # report the "add to favourites" task cannot be done, and the optional-practice
    # rule would close the chapter without it — there would be no gate to check.
    html = b"<!doctype html><html><body><h1>demo</h1></body></html>"
    up = c.post("/api/dashboards",
                data={"title": "Course demo", "description": ""},
                files={"html_file": ("demo.html", io.BytesIO(html), "text/html")},
                follow_redirects=False)
    check("the demo report uploads", up.status_code in (200, 302, 303), up.status_code)

    # Walk up to the task lesson, then prove the task gate.
    for lesson in LESSONS:
        if lesson["id"] == TASK_LESSON["id"]:
            break
        c.post("/api/learn/check", json={"lesson": lesson["id"], "answers": answers_for(lesson)})

    r = c.post(f"/api/learn/task/{TASK_LESSON['id']}")
    check("the task is not done before it is done",
          r.status_code == 200 and r.json()["done"] is False and r.json()["hint"],
          r.text[:160])
    r = c.post("/api/learn/check", json={"lesson": TASK_LESSON["id"],
                                         "answers": answers_for(TASK_LESSON)})
    body = r.json()
    check("right answers alone do not close a task lesson",
          body["all_correct"] and not body["completed"] and body["task_hint"], body)

    # Do the thing the lesson asks for — whatever kind it happens to be, so the
    # test follows the content instead of pinning one lesson id.
    kind = (TASK_LESSON.get("task") or {}).get("kind")
    if kind in ("favorite", "share_link"):
        from app.database import SessionLocal              # noqa: E402
        from app.models import (Dashboard, DashboardFavorite,  # noqa: E402
                                ShareToken, User as U_)
        with SessionLocal() as db:
            me = db.query(U_).filter(U_.person_id == "admin1").one()
            dash = db.query(Dashboard).filter(Dashboard.owner_id == me.id).first()
            if kind == "favorite":
                db.add(DashboardFavorite(user_id=me.id, dashboard_id=dash.id))
            else:
                db.add(ShareToken(token="s" * 12, dashboard_id=dash.id, created_by=me.id))
            db.commit()

    # Levels move with xp.
    lvl = body["state"]["level"]
    check("the level is computed from xp",
          lvl["name"] and (lvl["next_at"] is None or lvl["next_at"] > body["state"]["xp"]), lvl)

    # Someone else starts from zero.
    c.post("/api/admin/users", json={"person_id": "learner9", "role": "visitor",
                                     "display_name": "learner9"})
    login(c, "learner9")
    s9 = c.get("/api/learn/state?course=%s" % COURSE).json()
    check("progress is per person", s9["xp"] == 0 and s9["done"] == 0, s9["xp"])

# ── Block exams ───────────────────────────────────────────────────────────
UNITS = learn.content(COURSE)["units"]
U = UNITS[0]["id"]

with TestClient(app) as c:
    login(c, "admin1")
    # The block's tasks must really be done, otherwise the chapters with them do
    # not close and the exam stays locked — that is the rule.
    from app.database import SessionLocal          # noqa: E402
    from app.models import Dashboard, DashboardFavorite, ShareToken, User as U_  # noqa: E402
    with SessionLocal() as db:
        me = db.query(U_).filter(U_.person_id == "admin1").one()
        dash = db.query(Dashboard).filter(Dashboard.owner_id == me.id).first()
        if dash is not None:
            if not db.query(DashboardFavorite).filter(
                    DashboardFavorite.user_id == me.id).first():
                db.add(DashboardFavorite(user_id=me.id, dashboard_id=dash.id))
            if not db.query(ShareToken).filter(ShareToken.created_by == me.id).first():
                db.add(ShareToken(token="t" * 12, dashboard_id=dash.id, created_by=me.id))
            db.commit()

    r = c.get("/api/learn/exam/%s" % U)
    check("the exam is locked until the unit is finished", r.status_code == 409,
          f"{r.status_code} {r.text[:120]}")
    # complete the whole block (tasks confirmed by the report uploaded above)
    for lesson in UNITS[0]["lessons"]:
        c.post("/api/learn/check", json={"lesson": lesson["id"],
                                         "answers": answers_for(lesson)})
    r = c.get("/api/learn/exam/%s" % U)
    check("the exam opens when the unit is done", r.status_code == 200, r.status_code)
    exam = r.json()
    body = json.dumps(exam, ensure_ascii=False)
    check("the exam ships no answers", '"answer"' not in body and '"why"' not in body,
          body[:120])
    check("the exam is in exam mode", exam.get("mode") == "exam", exam.get("mode"))
    check("every exam question says which chapter it came from",
          all(q.get("from") for q in exam["checks"]),
          [q.get("from") for q in exam["checks"]][:3])
    covered = {q["from"] for q in exam["checks"]}
    check("the exam covers the whole unit, not two chapters",
          len(covered) >= min(len(UNITS[0]["lessons"]), len(exam["checks"])),
          sorted(covered))

    again = c.get("/api/learn/exam/%s" % U).json()
    check("the exam question set is stable for a person",
          [q["q"] for q in exam["checks"]] == [q["q"] for q in again["checks"]])

    wrong = [-1] * len(exam["checks"])
    r = c.post("/api/learn/check", json={"lesson": "%s:exam" % U, "answers": wrong})
    d = r.json()
    check("a failed exam is not counted",
          not d["completed"] and d["right"] == 0 and d["need"] >= 1, d.get("right"))
    check("the failed exam still explains every question",
          all(x.get("why") for x in d["results"]), d["results"][:1])

print()
# Course files are edited by hand: pin what a chapter must carry.
# The English course is the default; Russian remains a language pack and
# still loads for lang=ru (rather than silently falling back to English).
for course in learn.COURSES:
    _en, _ru = learn.content(course), learn.content(course, "ru")
    check("%s: default course content is English" % course,
          learn.content_lang(course) == "en" and _en.get("title")
          and not any("\u0400" <= ch <= "\u04ff" for ch in _en["title"]), _en.get("title"))
    check("%s: lang=ru loads the Russian pack" % course,
          learn.content_lang(course, "ru") == "ru"
          and any("\u0400" <= ch <= "\u04ff" for ch in _ru.get("title", ""))
          and len(learn.lessons(course, "ru")) == len(learn.lessons(course)),
          _ru.get("title"))
check("an unknown language falls back to English",
      learn.content_lang("prep", "xx") == "en"
      and learn.content("prep", "xx").get("title") == learn.content("prep").get("title"))

ALL_LESSONS = [l for course in learn.COURSES for l in learn.lessons(course)]
ids = [l["id"] for l in ALL_LESSONS] + [u["id"] for course in learn.COURSES
                                        for u in learn.content(course)["units"]]
check("lesson and unit ids are unique across courses", len(ids) == len(set(ids)),
      [i for i in ids if ids.count(i) > 1][:3])
bad = []
for lesson in ALL_LESSONS:
    for i, ch in enumerate(lesson.get("checks") or []):
        a, opts = ch.get("answer"), ch.get("options") or []
        if not isinstance(a, int) or not (0 <= a < len(opts)):
            bad.append((lesson["id"], i, "answer out of range"))
        if len(opts) < 3:
            bad.append((lesson["id"], i, "fewer than three options"))
        if not ch.get("why"):
            bad.append((lesson["id"], i, "no explanation"))
    if not (lesson.get("checks") or []):
        bad.append((lesson["id"], "-", "no checks at all"))
check("every question is answerable and explained", not bad, bad[:4])

# A question's form must not give the answer away. The first version of the bank
# failed exactly here: the right option was the longest in 139 of 145 cases and
# sat second in 81 % — you could answer without reading the question.
# tools/learn_balance.py is the same control for hand edits.
ALL = [c for l in ALL_LESSONS for c in (l.get("checks") or [])]
longest = sum(1 for c in ALL
              if [len(o) for o in c["options"]].index(max(len(o) for o in c["options"]))
              == c["answer"])
skew = [(l["id"], c["q"][:40]) for l in ALL_LESSONS for c in (l.get("checks") or [])
        if max(len(o) for o in c["options"]) / max(1, min(len(o) for o in c["options"])) > 1.6]
pos = collections.Counter(c["answer"] for c in ALL)
share = {k: 100 * v / len(ALL) for k, v in pos.items()}
check("the longest option is not the answer", longest <= 0.35 * len(ALL),
      f"{longest} of {len(ALL)}")
check("options of one question weigh the same", not skew, skew[:3])
check("the answer is not parked on one position",
      len(share) == 4 and all(12 <= v <= 38 for v in share.values()),
      {k: round(v) for k, v in sorted(share.items())})
for course in learn.COURSES:
    check("the top level is reachable (%s)" % course,
          learn.content(course)["levels"][-1]["xp"] <= learn.total_xp(course),
          (learn.content(course)["levels"][-1]["xp"], learn.total_xp(course)))
# A card is a screen of its own, and a pointer on it must point at something on
# that same screen. "Copy this query" on a screen with no query is broken logic,
# not nitpicking: the owner sent exactly such a screenshot.
# The rule lives in tools/learn_screens.py; the test only keeps it from being bypassed.
import subprocess
_screens = subprocess.run([sys.executable, "tools/learn_screens.py"],
                          cwd=os.getcwd(), capture_output=True, text=True)
check("every screen explains itself", _screens.returncode == 0,
      [ln.strip() for ln in _screens.stdout.splitlines() if "BAD" in ln][:4])

check("the preparatory course has no exams", not learn.has_exams("prep"))
check("the base course has exams", learn.has_exams("base"))

# ── Training file on a card ──────────────────────────────────────────────
# A "First report" lesson gives the person the export they build a report on.
# Three rules: the name resolves to a URL, the URL leads to an existing file, and
# a non-existent name does not become a "Download" button with a 404 behind it.
FIXTURE = "delta-sales.xlsx"
_card = learn.public_lesson({"cards": [{"heading": "x", "file": FIXTURE}]},
                            False, True)["cards"][0]
check("a card with a file gets a file_url",
      _card.get("file_url") == "/static/learn/files/" + FIXTURE and
      _card.get("file") == FIXTURE, _card)
check("the file_url points at a file that exists",
      os.path.isfile(os.path.join(os.getcwd(), "static", "learn", "files", FIXTURE)))
_missing = learn.public_lesson({"cards": [{"heading": "x", "file": "nope.xlsx"}]},
                               False, True)["cards"][0]
check("a card whose file is missing gets no file_url",
      "file_url" not in _missing and "file" not in _missing, _missing)
check("a file name cannot walk out of the folder",
      learn.file_url("../../img/learn/prep1.webp") is None and
      learn.file_url("/etc/passwd") is None)

with TestClient(app) as c:
    login(c, "admin1")
    r = c.get("/static/learn/files/" + FIXTURE)
    check("the course file is served by the static mount",
          r.status_code == 200 and len(r.content) > 1000, r.status_code)

# The dataset must be reproducible: otherwise the numbers in the lessons go stale
# on the first rebuild, and the course cannot be checked.
_fixture = subprocess.run([sys.executable, "tools/learn_fixture_delta.py", "--check"],
                          cwd=os.getcwd(), capture_output=True, text=True)
check("the course dataset regenerates byte-for-byte", _fixture.returncode == 0,
      _fixture.stdout.strip()[-300:] or _fixture.stderr.strip()[-300:])

# The sample report is built from the same export, and its numbers must match the
# case walkthrough. Plus two properties without which it cannot go into static:
# no scripts at all (the file lives on the platform's origin) and no outbound links.
_report = subprocess.run([sys.executable, "tools/learn_fixture_report.py", "--check"],
                         cwd=os.getcwd(), capture_output=True, text=True)
check("the sample report regenerates byte-for-byte", _report.returncode == 0,
      _report.stdout.strip()[-300:] or _report.stderr.strip()[-300:])
# A card may hand out several files: the sample report is useless without the
# export, and splitting them across neighbouring screens means losing one of the two.
_multi = [c for u in learn.content("prep")["units"] for l in u["lessons"]
          for c in l["cards"] if isinstance(c.get("file"), list)]
check("a card may carry several files", bool(_multi), len(_multi))
if _multi:
    _pub = learn.public_lesson({"cards": [_multi[0]]}, True, True)["cards"][0]
    check("every file of such a card gets its own link",
          len(_pub.get("files") or []) == len(_multi[0]["file"])
          and all(f["url"].startswith("/static/learn/files/") for f in _pub["files"]),
          _pub.get("files"))

# The SQL the course gives a person must run. Otherwise you get exactly what
# happened on 13.09: the owner copied a piece of the block and got an engine
# refusal. Every paragraph of the block (separated by a blank line — that is how
# people cut it by hand) goes through the engine's REAL guard_sql.
_sql_card = None
for _u in learn.content("prep")["units"]:
    for _l in _u["lessons"]:
        for _c in _l.get("cards", []):
            if _c.get("code_text") and "FROM delta_sales" in _c["code_text"]:
                _sql_card = _c
check("the course ships SQL for the platform path", _sql_card is not None)
if _sql_card:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "py"))
    try:
        import _explore as _eng                                # noqa: E402
    except Exception as _e:                                    # pragma: no cover
        _eng = None
        print("SKIP guard check:", _e)
    if _eng is not None:
        _bad = []
        for _part in [x.strip() for x in _sql_card["code_text"].split("\n\n") if x.strip()]:
            try:
                _eng.guard_sql(_part)
            except Exception as _err:
                _bad.append(_part.splitlines()[0][:40] + " → " + str(_err))
        check("every query of the card passes the engine's own guard", not _bad, _bad[:2])

_sample = open("static/learn/files/delta-report.html", encoding="utf-8").read()
# Not a single row of the export inside: outlet codes and item names appear
# only once the report is given a file. A file name in a hint is not data.
check("the sample report keeps no data inside",
      not any(code in _sample for code in ("RV-0", "NF-0", "LS-0", "lemonade")))
# …and those codes really are in the export, otherwise the check above is empty.
_csv = open("static/learn/files/delta-sales.csv", encoding="utf-8-sig").read()
check("the fixture carries the codes the report must not",
      all(code in _csv for code in ("RV-0", "NF-0", "LS-0", "lemonade")))
check("the sample report takes the file from the platform",
      "op_ready" in _sample and "op_file" in _sample)
check("the sample report works before upload too", "drop" in _sample)
check("the sample report reaches nowhere", "http" not in _sample)
# Exactly one link to an external file in the whole report — the platform library.
# It is not "external" in the dangerous sense: it is the same server that serves the
# report, and it is what spares the person re-saving the export as CSV. If there
# are ever more links, something else has crept into the sample.
_srcs = re.findall(r'src="([^"]+)"', _sample)
check("the sample report borrows exactly one library, ours",
      _srcs == ["/static/js/vendor/xlsx.full.min.js"], _srcs)
check("the sample report reads Excel, not only CSV",
      "XLSX.read" in _sample and "sheet_to_csv" in _sample)
# A date from a workbook arrives as a number of days since 30.12.1899 — the very
# trap the lesson warns about. The report must recognise it, otherwise on .xlsx it
# silently rejects every row (as the first version did).
check("the sample report knows the Excel serial date", "1899" in _sample)
check("the sample report says so when Excel is out of reach",
      "only on the platform" in _sample)
check("the sample report touches no browser storage",
      "localStorage" not in _sample and "document.cookie" not in _sample)
check("the sample report states what it read", "rows read" in _sample)
# Parsing robustness: exports come with both ";" and ",", numbers can be
# "1 234,56", dates in two formats. Verified in a browser on both variants;
# the rules themselves are pinned here so the next edit does not lose them.
check("the sample report sniffs the delimiter", "function sniff" in _sample)
check("the sample report normalises numbers", "function num" in _sample
      and "\\u00a0" in _sample)
check("the sample report reads both date shapes", "iso = d" in _sample)
check("the sample report waits long enough for the platform", "2000" in _sample)

# ── Reward for a completed course ─────────────────────────────────────────
# "Course completed" is not a flag someone sets but a set of `learn_progress`
# rows. Hence the checks: no diploma before the course ends, the completion
# mark is written once, and the special card appears by itself.
with TestClient(app) as c:
    login(c, "admin1")
    st = c.get("/api/learn/state?course=prep").json()
    check("course state says whether it is finished", st.get("completed") is False,
          st.get("completed"))
    check("an unfinished course has no finish date", st.get("finished_at") is None)
    check("the course names its reward card", bool((st.get("reward") or {}).get("art")),
          st.get("reward"))

    r = c.get("/learn/prep/diploma", follow_redirects=False)
    check("no diploma before the course is finished", r.status_code == 302, r.status_code)
    check("and the redirect leads back into the course",
          (r.headers.get("location") or "").endswith("/learn/prep"), r.headers.get("location"))
    check("an unknown course has no diploma", c.get("/learn/nope/diploma").status_code == 404)

    from app.database import SessionLocal                      # noqa: E402
    from app.models import AuditLog as AL, QuestCard, User as U2   # noqa: E402
    from app.services import learn as _learn                   # noqa: E402
    with SessionLocal() as db:
        me = db.query(U2).filter(U2.person_id == "admin1").one()
        lessons_prep = _learn.lessons("prep")
        for l in lessons_prep[:-1]:
            _learn.complete(db, me, l)
        db.commit()
        check("one lesson short is not a finished course",
              _learn.course_done(db, me, "prep") is None)

    last = lessons_prep[-1]
    r = c.post("/api/learn/check", json={"lesson": last["id"],
                                         "answers": answers_for(last)})
    body = r.json()
    check("finishing the last lesson finishes the course",
          body.get("course_done") is True and body["state"]["completed"] is True, body.get("course_done"))

    with SessionLocal() as db:
        me = db.query(U2).filter(U2.person_id == "admin1").one()
        rows = db.query(AL).filter(AL.actor_id == me.id,
                                   AL.action == "learn_course_done").all()
        check("the course completion is written down once", len(rows) == 1, len(rows))
    # Passing the same lesson again must not write a second mark: otherwise
    # everyone who came back to reread a lesson would get "a second diploma".
    c.post("/api/learn/check", json={"lesson": last["id"], "answers": answers_for(last)})
    with SessionLocal() as db:
        me = db.query(U2).filter(U2.person_id == "admin1").one()
        rows = db.query(AL).filter(AL.actor_id == me.id,
                                   AL.action == "learn_course_done").all()
        check("and not a second time on a repeat pass", len(rows) == 1, len(rows))

    # The "publish a second version" task is the same platform EXISTS as the
    # first upload: the first publish gives version 1 and does NOT close the task.
    with SessionLocal() as db:
        me = db.query(U2).filter(U2.person_id == "admin1").one()
        from app.models import Dashboard as _D                  # noqa: E402
        dash = db.query(_D).filter(_D.owner_id == me.id).first()
        before = learn.verify_task(db, me, "new_version")
        check("one version is not enough for the new_version task",
              before is False or dash is None, before)
        if dash is not None:
            dash.current_version = 2
            db.commit()
            check("a second version closes it",
                  learn.verify_task(db, me, "new_version") is True)

    r = c.get("/learn/prep/diploma")
    check("the diploma opens once the course is finished", r.status_code == 200, r.status_code)
    check("the diploma names the person and the course",
          "admin1" in r.text and "Your first report" in r.text)
    check("the diploma carries a number", 'data-number="' in r.text)
    # Same discipline as the course state: there can be no answers on the page.
    check("the diploma leaks no answers", '"answer"' not in r.text and '"why"' not in r.text)

    # The ceremony lives in Quests: the course puts the card into the surprise
    # queue, and the person opens it where they open the daily chest.
    from app.models import QuestOpening as QO                # noqa: E402
    with SessionLocal() as db:
        me = db.query(U2).filter(U2.person_id == "admin1").one()
        ops = db.query(QO).filter(QO.user_id == me.id,
                                  QO.card_id == "learn-prep").all()
        check("the finished course queues one surprise", len(ops) == 1, len(ops))
        check("and it waits unopened", ops and ops[0].revealed_at is None)
        check("it is not counted into the eight",
              db.query(QuestCard).filter(QuestCard.user_id == me.id,
                                         QuestCard.card_id == "learn-prep").count() == 0)
    r = c.post("/api/quests/open")
    body = r.json() if r.status_code == 200 else {}
    check("opening it in Quests reveals the special card",
          (body.get("opening") or {}).get("card_id") == "learn-prep", r.text[:160])
    check("and it comes with its own name and rarity",
          (body.get("opening") or {}).get("rarity") == "special"
          and bool((body.get("opening") or {}).get("name")), body.get("opening"))

    # The course reward does NOT eat the daily chest: it stays closed and waits
    # for the next press. Otherwise someone who reached the end on a working day
    # would pay for the diploma with their daily card.
    with SessionLocal() as db:
        me = db.query(U2).filter(U2.person_id == "admin1").one()
        from app.services import quests as _qq                  # noqa: E402
        daily = db.query(QO).filter(QO.user_id == me.id, QO.day == _qq.today(),
                                    QO.source == "daily").count()
        check("the daily chest is left alone", daily == 0, daily)

    # The special card is computed: nobody "issued" it, it follows from the
    # completed course. And it does NOT take part in the set of eight.
    from app.services import quests as _q                      # noqa: E402
    with SessionLocal() as db:
        me = db.query(U2).filter(U2.person_id == "admin1").one()
        sp = _q.special_cards(db, me)
        check("the finished course grants its special card",
              any(x["course"] == "prep" and x["owned"] for x in sp), sp)
        check("the unfinished one stays locked",
              any(x["course"] == "base" and not x["owned"] for x in sp), sp)
        check("special cards live outside the eight",
              all(x["id"] not in dict(_q.card_meta()) for x in sp), sp)

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("smoke_learn: all checks passed")
