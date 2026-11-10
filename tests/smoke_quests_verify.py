"""Smoke: quest verification, client events, the chest, exchange, state
(app/services/quests.py, phase 3).

Actions go through the real routes (upload, favorite, presentations,
share): the test checks exactly the trail prod leaves, not
hand-inserted rows. The service functions themselves are called directly via
`SessionLocal`, because the quests router is phase 5.
"""
import os
import sys
import tempfile
from datetime import date, timedelta

TMP = tempfile.mkdtemp(prefix="op-quests-verify-")
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
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog, Employee, QuestCard, QuestOpening, QuestProfile, User,
)
from app.services import quests as q  # noqa: E402

FAILURES = []
q._HOLIDAYS_OVERRIDE = set()   # calendar without holidays, for determinism
# The test season starts "today", and rewards count from the local midnight
# of the start: on a weekend "today" moves back to the previous workday, otherwise
# the start would slide to Monday and everything done now would fall
# before the season.
if not q.is_working(q.today()):
    q._TODAY_OVERRIDE = q.prev_working(q.today())


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def make_dashboard(c, title):
    html = b"<html><body><h1>" + title.encode() + b"</h1></body></html>"
    r = c.post("/api/dashboards", data={"title": title, "description": ""},
               files={"html_file": ("d.html", html, "text/html")})
    assert r.status_code in (200, 303), f"create {title}: {r.status_code} {r.text[:300]}"
    loc = r.headers.get("location") or (r.history[-1].headers.get("location") if r.history else None)
    return loc.rstrip("/").rsplit("/", 1)[-1]


def with_profile(person_id, fn):
    """Open a session, find the user and their profile, run `fn`."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.person_id == person_id).first()
        profile = q.ensure_profile(db, user)
        out = fn(db, user, profile)
        db.commit()
        return out
    finally:
        db.close()


def points_of(person_id):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.person_id == person_id).first()
        p = db.query(QuestProfile).filter(QuestProfile.user_id == user.id).first()
        return int(p.points) if p else 0
    finally:
        db.close()


def openings_of(person_id):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.person_id == person_id).first()
        return [(o.source, o.card_id, o.day) for o in
                db.query(QuestOpening).filter(QuestOpening.user_id == user.id)
                .order_by(QuestOpening.id).all()]
    finally:
        db.close()


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        for oid, name in (("hist1", "Ivanov Ivan Ivanovich"), ("new1", "Peterson Peter"),
                          ("day1", "Sidorov Sidor"), ("lb1", "Kim Alia"),
                          ("lb2", "Lee Victor"), ("lb3", "Kim Dana")):
            r = c.post("/api/admin/users",
                       json={"person_id": oid, "role": "superuser", "display_name": name})
            assert r.status_code == 200, f"create {oid}: {r.status_code} {r.text[:200]}"

        # ── (a) historical user: everything done BEFORE the first verification ──
        login(c, "hist1")
        d1 = make_dashboard(c, "Hist Report One")
        d2 = make_dashboard(c, "Hist Report Two")

        r = c.post("/api/presentations", json={"title": "Quarter results"})
        check("presentation created", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        pres_id = r.json()["id"]
        for d in (d1, d2):
            r = c.post(f"/api/presentations/{pres_id}/items",
                       json={"kind": "dashboard", "dashboard_id": d})
            check(f"slide {d[:6]} added", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        r = c.put(f"/api/dashboards/{d1}/favorite")
        check("favorite added", r.status_code == 200, r.text[:200])
        r = c.post(f"/api/dashboards/{d1}/share-links", json={"label": "for colleagues"})
        check("share link created", r.status_code == 200, r.text[:200])

        # The season has not started yet: nothing done so far counts,
        # but the profile gets marked and the cursor moves to the season start.
        from app.config import settings
        saved_start = settings.quests_season_start
        settings.quests_season_start = q.next_working(q.today() + timedelta(days=1)).isoformat()
        try:
            awards = with_profile("hist1", lambda db, u, p: q.verify(db, u, p))
            check("pre-season deeds award nothing", awards == [], str(awards))
            check("pre-season points stay 0", points_of("hist1") == 0, str(points_of("hist1")))
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.person_id == "hist1").first()
                p = db.query(QuestProfile).filter(QuestProfile.user_id == user.id).first()
                check("pre-season: backfilled flag set", p.backfilled is True, str(p.backfilled))
                check("pre-season: cursor at season start",
                      p.verified_at == q.season_start_utc(), f"{p.verified_at} vs {q.season_start_utc()}")
            finally:
                db.close()
        finally:
            settings.quests_season_start = saved_start

        # The season started today: the same actions are now in the window and count via the audit log.
        settings.quests_season_start = q.today().isoformat()
        awards = with_profile("hist1", lambda db, u, p: q.verify(db, u, p))
        ids = sorted(a["quest_id"] for a in awards)
        check("in-season deeds awarded pres/fav/share", ids == ["fav", "pres", "share"], str(ids))
        check("in-season points 20+10+15", points_of("hist1") == 45, str(points_of("hist1")))
        check("each quest gave a surprise opening",
              sorted(s for s, _c, _d in openings_of("hist1")) ==
              ["quest:fav", "quest:pres", "quest:share"], str(openings_of("hist1")))
        check("all three awards carry an opening",
              all(a["opening"] and a["opening"]["card_id"] for a in awards), str(awards))
        check("awards are surprises",
              all(a["surprise"] is True for a in awards), str(awards))

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.person_id == "hist1").first()
            p = db.query(QuestProfile).filter(QuestProfile.user_id == user.id).first()
            check("backfilled flag set", p.backfilled is True, str(p.backfilled))
            check("verified_at set", p.verified_at is not None, str(p.verified_at))
            row = db.query(AuditLog).filter(AuditLog.actor_id == user.id,
                                            AuditLog.action == "quests_backfill").first()
            check("audit quests_backfill written", row is not None)
            check("quests_backfill carries since and no awards",
                  row is not None and row.get_detail().get("awarded") == []
                  and row.get_detail().get("policy") == "season_only",
                  str(row.get_detail() if row else None))
        finally:
            db.close()

        again = with_profile("hist1", lambda db, u, p: q.verify(db, u, p))
        check("second verify awards nothing", again == [], str(again))
        check("second verify did not add points", points_of("hist1") == 45, str(points_of("hist1")))

        # ── (b) new user: incremental audit scan ────────────────────────────
        login(c, "new1")
        nd = make_dashboard(c, "New Report")
        first = with_profile("new1", lambda db, u, p: q.verify(db, u, p))
        check("new user: first verify awards nothing", first == [], str(first))
        check("new user: zero points", points_of("new1") == 0, str(points_of("new1")))

        r = c.put(f"/api/dashboards/{nd}/favorite")
        check("new user favorited", r.status_code == 200, r.text[:200])
        aw = with_profile("new1", lambda db, u, p: q.verify(db, u, p))
        check("new user: fav awarded from audit",
              len(aw) == 1 and aw[0]["quest_id"] == "fav", str(aw))
        check("new user: fav is worth 10", aw and aw[0]["points"] == 10, str(aw))
        check("new user: surprise opening quest:fav",
              [s for s, _c, _d in openings_of("new1")] == ["quest:fav"], str(openings_of("new1")))
        check("new user: points 10", points_of("new1") == 10, str(points_of("new1")))
        aw2 = with_profile("new1", lambda db, u, p: q.verify(db, u, p))
        check("new user: repeat verify awards nothing", aw2 == [], str(aw2))
        check("new user: points still 10", points_of("new1") == 10, str(points_of("new1")))

        # ── (c) client events: points only, exactly once ────────────────────
        ev1 = with_profile("new1", lambda db, u, p: q.record_client_event(db, p, "theme"))
        check("theme event first time", ev1["already"] is False and ev1["award"], str(ev1))
        check("theme award has no opening", ev1["award"]["opening"] is None, str(ev1))
        check("theme award not a surprise", ev1["award"]["surprise"] is False, str(ev1))
        check("theme gave 10 points", points_of("new1") == 20, str(points_of("new1")))
        ev2 = with_profile("new1", lambda db, u, p: q.record_client_event(db, p, "theme"))
        check("theme event second time -> already", ev2["already"] is True, str(ev2))
        check("theme did not pay twice", points_of("new1") == 20, str(points_of("new1")))
        check("no opening from client events",
              [s for s, _c, _d in openings_of("new1")] == ["quest:fav"], str(openings_of("new1")))
        ev3 = with_profile("new1", lambda db, u, p: q.record_client_event(db, p, "view"))
        check("view event awards grid",
              ev3["award"] and ev3["award"]["quest_id"] == "grid", str(ev3))
        try:
            with_profile("new1", lambda db, u, p: q.record_client_event(db, p, "nope"))
            check("invalid kind raises ValueError", False, "no exception")
        except ValueError:
            check("invalid kind raises ValueError", True)

        # ── (d) daily chest and streak ──────────────────────────────────────
        days = [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10),
                date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)]
        q._TODAY_OVERRIDE = days[0]
        res = with_profile("day1", lambda db, u, p: q.open_daily(db, p))
        check("day 1: daily opening", res["opening"]["source"] == "daily", str(res["opening"]))
        check("day 1: streak 1", res["streak"] == 1, str(res["streak"]))
        check("day 1: event first", res["event"] == "first", res["event"])
        check("day 1: no extra opening", res["extra_opening"] is None, str(res["extra_opening"]))
        try:
            with_profile("day1", lambda db, u, p: q.open_daily(db, p))
            check("day 1: second open raises AlreadyOpened", False, "no exception")
        except q.AlreadyOpened:
            check("day 1: second open raises AlreadyOpened", True)

        q._TODAY_OVERRIDE = days[1]
        res = with_profile("day1", lambda db, u, p: q.open_daily(db, p))
        check("day 2: streak 2", res["streak"] == 2, str(res["streak"]))
        check("day 2: event continued", res["event"] == "continued", res["event"])

        q._TODAY_OVERRIDE = days[2]
        res = with_profile("day1", lambda db, u, p: q.open_daily(db, p))
        check("day 3: streak 3", res["streak"] == 3, str(res["streak"]))
        check("day 3: rare_x2 milestone", res["milestones"]["rare_x2"] is True, str(res["milestones"]))
        check("day 3: no extra opening yet", res["extra_opening"] is None, str(res["extra_opening"]))

        for day in days[3:6]:
            q._TODAY_OVERRIDE = day
            with_profile("day1", lambda db, u, p: q.open_daily(db, p))
        q._TODAY_OVERRIDE = days[6]
        res = with_profile("day1", lambda db, u, p: q.open_daily(db, p))
        check("day 7: streak 7", res["streak"] == 7, str(res["streak"]))
        check("day 7: extra_surprise milestone",
              res["milestones"]["extra_surprise"] is True, str(res["milestones"]))
        check("day 7: second opening source=milestone",
              res["extra_opening"] and res["extra_opening"]["source"] == "milestone",
              str(res["extra_opening"]))
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.person_id == "day1").first()
            p = db.query(QuestProfile).filter(QuestProfile.user_id == u.id).first()
            check("best_streak tracked", p.best_streak == 7, str(p.best_streak))
            check("last_open_date is the overridden day", p.last_open_date == days[6],
                  str(p.last_open_date))
            n = db.query(QuestOpening).filter(QuestOpening.user_id == u.id).count()
            check("8 openings total (7 daily + 1 milestone)", n == 8, str(n))
        finally:
            db.close()

        # ── (e) duplicate exchange ──────────────────────────────────────────
        def set_card(person_id, card_id, count):
            db = SessionLocal()
            try:
                u = db.query(User).filter(User.person_id == person_id).first()
                row = db.query(QuestCard).filter(QuestCard.user_id == u.id,
                                                 QuestCard.card_id == card_id).first()
                if row is None:
                    row = QuestCard(user_id=u.id, card_id=card_id, count=count)
                    db.add(row)
                else:
                    row.count = count
                db.commit()
            finally:
                db.close()

        # Exchange lives only in the ACTIVE season, so use its cards.
        DUP = q.season_card_ids()[0]
        OTHER = q.season_card_ids()[-2]
        set_card("day1", DUP, 4)
        try:
            with_profile("day1", lambda db, u, p: q.exchange_duplicates(db, p, DUP))
            check("exchange with 4 copies raises", False, "no exception")
        except ValueError as e:
            check("exchange with 4 copies raises", "Five matching cards" in str(e), str(e))

        set_card("day1", DUP, 5)
        ex1 = with_profile("day1", lambda db, u, p: q.exchange_duplicates(db, p, DUP))
        check("exchange -> dup:1", ex1["opening"]["source"] == "dup:1", str(ex1["opening"]))
        check("exchange leaves 0 (or 1 if the very same card dropped)",
              ex1["remaining"] == (1 if ex1["opening"]["card_id"] == DUP else 0), str(ex1))

        set_card("day1", DUP, 5)
        ex2 = with_profile("day1", lambda db, u, p: q.exchange_duplicates(db, p, DUP))
        check("second exchange same day -> dup:2", ex2["opening"]["source"] == "dup:2",
              str(ex2["opening"]))
        try:
            with_profile("day1", lambda db, u, p: q.exchange_duplicates(db, p, OTHER))
            check("exchange of a card never drawn raises", False, "no exception")
        except ValueError:
            check("exchange of a card never drawn raises", True)

        # ── (f) state and leaderboard ───────────────────────────────────────
        st = with_profile("day1", lambda db, u, p: q.state(db, u, p))
        need = {"today", "opened_today", "openings_today", "streak", "best_streak",
                "freeze_available", "path", "points", "level", "cards", "quests",
                "chances", "quiz_correct_total", "leaderboard_optin"}
        check("state has every documented key", need <= set(st), str(sorted(need - set(st))))
        check("state has no leaderboard/quiz keys (API merges them)",
              "leaderboard" not in st and "quiz" not in st, str(sorted(st)))
        check("state.today is the overridden day", st["today"] == days[6].isoformat(), st["today"])
        check("state.opened_today true", st["opened_today"] is True)
        check("state.cards covers all CARDS",
              set(st["cards"]) == {cid for cid, _r, _w in q.CARDS}, str(st["cards"]))
        check("state.quests lists the five onboarding quests",
              [x["id"] for x in st["quests"]] == ["pres", "fav", "theme", "grid", "share"],
              str([x["id"] for x in st["quests"]]))
        check("state.path is JSON-safe (dates are strings)",
              all(isinstance(step["day"], str) for step in st["path"]), str(st["path"][:1]))
        import json as _json
        try:
            _json.dumps(st)
            check("state is JSON-serialisable", True)
        except TypeError as e:
            check("state is JSON-serialisable", False, str(e))
        stn = with_profile("new1", lambda db, u, p: q.state(db, u, p))
        check("state marks done quests",
              [x["id"] for x in stn["quests"] if x["done"]] == ["fav", "theme", "grid"],
              str([(x["id"], x["done"]) for x in stn["quests"]]))
        check("done quest carries completed_at",
              all(x["completed_at"] for x in stn["quests"] if x["done"]), str(stn["quests"]))

        # leaderboard: three profiles with different points, one has no Employee row
        db = SessionLocal()
        try:
            for oid, pts in (("lb1", 300), ("lb2", 200), ("lb3", 100)):
                u = db.query(User).filter(User.person_id == oid).first()
                db.add(QuestProfile(user_id=u.id, points=pts, leaderboard_optin=True))
            db.add(Employee(id=901, person_id="lb1", deptname_ru="Data Team"))
            db.add(Employee(id=902, person_id="lb2", deptname_ru="Finance"))
            db.commit()
        finally:
            db.close()

        lb = with_profile("lb1", lambda db, u, p: q.leaderboard(db, u, p))
        check("leaderboard has percentile/total/median",
              {"percentile", "total", "median"} <= set(lb), str(sorted(lb)))
        check("top scorer percentile is high", lb["percentile"] >= 80, str(lb))
        check("opted-in user gets rows", isinstance(lb.get("rows"), list) and lb["rows"], str(lb))
        check("rows are ranked by points",
              [r_["points"] for r_ in lb["rows"]] == sorted((r_["points"] for r_ in lb["rows"]),
                                                            reverse=True), str(lb["rows"]))
        check("me flag on own row",
              [r_["me"] for r_ in lb["rows"]].count(True) == 1, str(lb["rows"]))
        check("me_rank present", lb.get("me_rank") == 1, str(lb.get("me_rank")))
        by_name = {r_["name"]: r_ for r_ in lb["rows"]}
        check("short name is 'Surname I.'", "Kim A." in by_name, str(sorted(by_name)))
        check("dept from Employee", by_name.get("Kim A.", {}).get("dept") == "Data Team",
              str(by_name.get("Kim A.")))
        check("user without an Employee row gets dept None",
              by_name.get("Kim D.", {}).get("dept") is None, str(by_name.get("Kim D.")))

        lb3 = with_profile("lb3", lambda db, u, p: q.leaderboard(db, u, p))
        check("lowest opted-in user ranks last",
              lb3.get("me_rank") == 3, str(lb3.get("me_rank")))

        # without consent: percentile only
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.person_id == "lb2").first()
            p = db.query(QuestProfile).filter(QuestProfile.user_id == u.id).first()
            p.leaderboard_optin = False
            db.commit()
        finally:
            db.close()
        lb2 = with_profile("lb2", lambda db, u, p: q.leaderboard(db, u, p))
        check("opt-out sees no rows", "rows" not in lb2 and "me_rank" not in lb2, str(sorted(lb2)))
        check("opt-out still sees percentile", isinstance(lb2["percentile"], int), str(lb2))

        # ...and the opt-out disappears from OTHER people's tables too, while
        # still counting toward the population the percentile is measured
        # against: opting out hides the NAME, it does not leave the season.
        lb_after = with_profile("lb1", lambda db, u, p: q.leaderboard(db, u, p))
        names_after = [r_["name"] for r_ in lb_after["rows"]]
        check("opted-out user is absent from another user's rows",
              "Lee V." not in names_after, str(names_after))
        check("but still counted in total", lb_after["total"] == lb["total"],
              (lb["total"], lb_after["total"]))
        check("ranks stay contiguous after the opt-out",
              [r_["rank"] for r_ in lb_after["rows"]] == list(range(1, len(names_after) + 1)),
              str(lb_after["rows"]))

    q._TODAY_OVERRIDE = None
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
