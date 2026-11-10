"""Smoke test: "Quests" collectible card seasons (season 1 is foxes,
season 2 is frogs) and the companion (ADR-17b) bound to the SEASON 1 set.

The test does NOT depend on which season is active by default: everything
that needs a specific active season sets it via the overlay
(`quests_config.set_overrides`, like the admin panel) and resets it in `finally`.

The live mine this test pins: `owned_count_db` once counted all
cards without filtering by set, so four foxes + four frogs gave
"8 of 8" and a companion for an incomplete set. Same for the "people with
a companion" counter in `quests_stats`.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

TMP = tempfile.mkdtemp(prefix="op-quests-seasons-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_QUESTS_ENABLED": "true",
    "OP_SAML_SP_BASE_URL": "http://testserver",
})
sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import QuestCard, User  # noqa: E402
from app.services import quests, quests_config  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


FOX = quests.season_card_ids(1)
FROG = quests.season_card_ids(2)


def mk_user(db, person_id):
    u = User(person_id=person_id, display_name=person_id, role="visitor", is_active=True)
    db.add(u)
    db.commit()
    return u


def give(db, uid, ids, count=1):
    db.query(QuestCard).filter(QuestCard.user_id == uid).delete()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for cid in ids:
        db.add(QuestCard(user_id=uid, card_id=cid, count=count, first_at=now, last_at=now))
    db.commit()


def state_of(db, user):
    prof = quests.ensure_profile(db, user)
    db.commit()
    return quests.state(db, user, prof), prof


def main():
    with TestClient(app):
        db = SessionLocal()
        try:
            # ── registry ──────────────────────────────────────────────
            check("season 1 and season 2 have eight cards each", len(FOX) == 8 and len(FROG) == 8,
                  (len(FOX), len(FROG)))
            check("season sets do not overlap by card_id", not (set(FOX) & set(FROG)),
                  set(FOX) & set(FROG))
            check("card_ids are unique within a season",
                  len(set(FOX)) == 8 and len(set(FROG)) == 8)
            check("frogs are frog-*", all(c.startswith("frog-") for c in FROG), FROG)
            check("companion is bound to season 1", quests.COMPANION_SEASON == 1)

            full_fox = mk_user(db, "fox-full")
            mixed = mk_user(db, "fox4-frog4")
            frogs = mk_user(db, "frog-full")
            give(db, full_fox.id, FOX)
            give(db, mixed.id, FOX[:4] + FROG[:4])
            give(db, frogs.id, FROG)

            # ── companion: owned_count_db ─────────────────────────────
            check("owned_count_db: full season 1 = 8",
                  quests.owned_count_db(db, full_fox.id) == 8,
                  quests.owned_count_db(db, full_fox.id))
            check("owned_count_db: 4 foxes + 4 frogs = 4, not 8",
                  quests.owned_count_db(db, mixed.id) == 4, quests.owned_count_db(db, mixed.id))
            check("owned_count_db: eight frogs count 0 for the companion",
                  quests.owned_count_db(db, frogs.id) == 0, quests.owned_count_db(db, frogs.id))
            check("owned_count_db for season 2 counts frogs",
                  quests.owned_count_db(db, mixed.id, 2) == 4
                  and quests.owned_count_db(db, frogs.id, 2) == 8)

            # ── stats counter "people with a companion" ───────────────
            companions = _stats_companions(db)
            check("quests_stats: one companion (full season 1), "
                  "not three (4+4 and 8 frogs do not count)", companions == 1, companions)

            # ── companion and state() with season 2 active ────────────
            try:
                for active in (1, 2):
                    quests_config.set_overrides({"quests_season": active})
                    tag = f"[season {active} active] "
                    check(tag + "active_season()", quests.active_season()["key"] == active)

                    st, prof = state_of(db, full_fox)
                    check(tag + "full season 1: companion present", bool(st.get("companion")),
                          st.get("companion"))
                    quests.companion_touch(db, full_fox, prof)
                    db.commit()
                    check(tag + "companion_touch sets the mark", prof.companion_since is not None)
                    st, _ = state_of(db, mixed)
                    check(tag + "4 foxes + 4 frogs: NO companion", st.get("companion") is None,
                          st.get("companion"))
                    check(tag + "4+4: companion_touch does not set the mark",
                          not quests.companion_touch(db, mixed, quests.ensure_profile(db, mixed))
                          and quests.ensure_profile(db, mixed).companion_since is None)
                    st, _ = state_of(db, frogs)
                    check(tag + "8 frogs: NO companion", st.get("companion") is None,
                          st.get("companion"))

                    # state(): the seasons key
                    st, _ = state_of(db, mixed)
                    check(tag + "state: active_season", st.get("active_season") == active,
                          st.get("active_season"))
                    seasons = {s["key"]: s for s in st.get("seasons") or []}
                    check(tag + "state: seasons for both seasons", set(seasons) == {1, 2},
                          sorted(seasons))
                    check(tag + "state: total of 8 each", all(s.get("total") == 8 for s in seasons.values()),
                          {k: s.get("total") for k, s in seasons.items()})
                    check(tag + "state: owned foxes 4 / frogs 4",
                          seasons[1].get("owned") == 4 and seasons[2].get("owned") == 4,
                          {k: s.get("owned") for k, s in seasons.items()})
                    check(tag + "state: active only on the active season",
                          [k for k, s in seasons.items() if s.get("active")] == [active],
                          {k: s.get("active") for k, s in seasons.items()})
                    check(tag + "state: companion only on season 1",
                          [k for k, s in seasons.items() if s.get("companion")] == [1])
                    st, _ = state_of(db, frogs)
                    seasons = {s["key"]: s for s in st["seasons"]}
                    check(tag + "state 8 frogs: owned 0 / 8",
                          seasons[1]["owned"] == 0 and seasons[2]["owned"] == 8,
                          {k: s["owned"] for k, s in seasons.items()})
                    check(tag + "state: cards of the active season are exactly its set",
                          set(st["cards"]) == set(quests.season_card_ids(active)), sorted(st["cards"]))

                    # the draw hands out the active season only
                    got = {quests.draw(s % 11, seed=f"{active}-{s}")["card_id"] for s in range(600)}
                    allowed = set(quests.season_card_ids(active))
                    check(tag + "draw hands out only active-season cards",
                          got <= allowed and len(got) >= 2, sorted(got - allowed))
                    if active == 2:
                        check(tag + "draw: frogs only", all(c.startswith("frog-") for c in got))
                    else:
                        check(tag + "draw: no frogs", not any(c.startswith("frog-") for c in got))

                    # duplicate exchange: a closed season refuses and deducts nothing
                    closed = 1 if active == 2 else 2
                    cid = quests.season_card_ids(closed)[0]
                    give(db, mixed.id, [cid], count=5)
                    prof = quests.ensure_profile(db, mixed)
                    refused = False
                    try:
                        quests.exchange_duplicates(db, prof, cid)
                    except ValueError:
                        refused = True
                    db.rollback()
                    left = db.query(QuestCard.count).filter(
                        QuestCard.user_id == mixed.id, QuestCard.card_id == cid).scalar()
                    check(tag + f"exchange of a closed season {closed} card: refused", refused)
                    check(tag + "closed-season exchange deducted nothing", left == 5, left)
                    give(db, mixed.id, FOX[:4] + FROG[:4])
            finally:
                quests_config.set_overrides({}, reset=["quests_season"])

            # ── Catch-up card on the switch day ──────────────────────────
            # Someone opened the surprise in the morning while foxes were active; during the day
            # frogs were switched on. They must not be left without their first frog, and must not
            # get it twice.
            try:
                quests_config.set_overrides({"quests_season": 1})
                late = mk_user(db, "late-switch")
                prof = quests.ensure_profile(db, late)
                d = quests.today()
                morning = quests.grant_opening(db, prof.user_id, d, "daily", 1)
                db.commit()
                check("morning card is from the fox season",
                      morning is not None and morning.card_id in set(quests.season_card_ids(1)))

                quests_config.set_overrides({"quests_season": 2})
                got = quests.grant_season_opener(db, late, prof)
                db.commit()
                check("catch-up card granted", got is not None)
                check("catch-up card is from the new season",
                      got is not None and got.card_id in set(quests.season_card_ids(2)))
                check("catch-up card waits to be opened instead of failing silently",
                      got is not None and got.revealed_at is None)
                check("catch-up card is not granted twice",
                      quests.grant_season_opener(db, late, prof) is None)

                # and someone who has not opened anything today has nothing to catch up on:
                # they get a frog through the regular daily surprise
                fresh = mk_user(db, "not-opened-yet")
                pf = quests.ensure_profile(db, fresh)
                db.commit()
                check("someone who has not opened today gets no catch-up card",
                      quests.grant_season_opener(db, fresh, pf) is None)
            finally:
                quests_config.set_overrides({}, reset=["quests_season"])
        finally:
            db.close()


def _stats_companions(db):
    """The "people with a companion" counter from the admin stats (key summary.companions)."""
    from fastapi.testclient import TestClient as _TC
    with _TC(app) as c:
        c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
        r = c.get("/api/admin/quests/stats")
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        return r.json()["summary"].get("companions")


main()
print()
print("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}")
sys.exit(1 if FAILURES else 0)

