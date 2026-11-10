"""Quest stats in the admin panel: access, rendering, content after card openings."""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-quests-stats-")
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

# Surprise of the day and the quiz run on workdays only: on a weekend the test looks
# at the nearest workday, otherwise it would fail every Saturday.
if not qsvc.is_working(qsvc.today()):
    qsvc._TODAY_OVERRIDE = qsvc.next_working(qsvc.today())

qsvc._HOLIDAYS_OVERRIDE = set()
FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


with TestClient(app) as c:
    login(c, "admin1")
    r = c.get("/admin/quests/stats")
    check("empty stats page renders", r.status_code == 200 and "Does the mechanic work" in r.text, r.status_code)
    check("empty: nobody yet", "No one has visited yet" in r.text)
    r = c.post("/api/admin/users", json={"person_id": "st1", "role": "superuser", "display_name": "Sanders Alice"})
    assert r.status_code == 200, r.text[:200]
    r = c.post("/api/admin/users", json={"person_id": "st2", "role": "visitor", "display_name": "Lee Victor"})
    assert r.status_code == 200, r.text[:200]

    # st1 opens today's surprise and a theme event; st2 only visits
    login(c, "st1")
    r = c.post("/api/quests/open", json={})
    check("st1 opened daily", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    r = c.post("/api/quests/event", json={"kind": "theme"})
    check("st1 theme event", r.status_code == 200, r.status_code)
    login(c, "st2")
    check("st2 visits quests", c.get("/quests").status_code == 200)
    r = c.get("/admin/quests/stats")
    check("visitor gets no stats page", r.status_code in (302, 403), r.status_code)
    check("visitor gets no stats json", c.get("/api/admin/quests/stats").status_code in (302, 403))

    login(c, "admin1")
    r = c.get("/api/admin/quests/stats")
    check("stats json 200", r.status_code == 200, r.status_code)
    j = r.json()
    m = j["summary"]
    check("participants = 2 (st1, st2)", m["participants"] == 2, str(m))
    check("opened today = 1", m["opened_today"] == 1, str(m))
    check("never opened = 1", m["never_opened"] == 1, str(m))
    check("quests total = 1 (theme)", m["quests_total"] == 1, str(m))
    check("today rows list st1 daily", any(r_["person_id"] == "st1" and r_["source"] == "daily" for r_ in j["today_rows"]), str(j["today_rows"]))
    people = {p["person_id"]: p for p in j["people"]}
    check("st1 has points and 1 day", people["st1"]["points"] > 0 and people["st1"]["days"] == 1 and people["st1"]["today"] is True, str(people.get("st1")))
    check("st2 zero", people["st2"]["points"] == 0 and people["st2"]["days"] == 0, str(people.get("st2")))
    check("timeline has today with daily=1", any(t["day"] == str(j["today"]) and t["daily"] == 1 and t["new"] == 1 for t in j["timeline"]), str(j["timeline"]))
    check("dist: one user-day with 1 opening", m["dist"]["1"] == 1, str(m["dist"]))
    r = c.get("/admin/quests/stats")
    check("stats page renders names", r.status_code == 200 and "Sanders Alice" in r.text and "Lee Victor" in r.text, r.status_code)
    check("stats page: today row with daily badge", 'qs-src daily' in r.text)
    check("admin quests page links to stats", 'href="/admin/quests/stats"' in c.get("/admin/quests").text)

print("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
