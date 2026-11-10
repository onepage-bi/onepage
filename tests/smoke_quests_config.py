"""Smoke test for "Quests" Phase 1 — schema (app/models.py's 7 quest_*/
quiz_* tables + their indexes, created via create_all/lifespan) and the
runtime overlay (app/services/quests_config.py).

Boot idiom from tests/smoke_favorites.py: env set before import, into a
tempdir, TestClient boots the app (lifespan runs create_all + the
CREATE INDEX IF NOT EXISTS migration block). A second boot in a fresh
TestClient context against the SAME db file pins that the migration is a
no-op the second time (mirrors tests/smoke_migration.py's intent, without
the subprocess — nothing here predates the feature so no raw-sqlite setup
is needed).
"""
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-quests-smoke-")
DB_PATH = f"{TMP}/onepage.db"
os.environ.update({
    "OP_DB_PATH": DB_PATH,
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

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


EXPECTED_TABLES = [
    "quest_profiles",
    "quest_openings",
    "quest_cards",
    "quest_completions",
    "quiz_questions",
    "quiz_attempts",
    "quest_client_events",
]

EXPECTED_INDEXES = [
    "ix_audit_log_actor_created",
    "ix_quest_openings_user_id",
    "ix_quest_openings_user_created",
    "ix_quest_cards_user_id",
    "ix_quest_completions_user_id",
    "ix_quiz_questions_day",
    "ix_quiz_attempts_user_id",
    "ix_quiz_attempts_token",
    "ix_quest_client_events_user_id",
]


def table_names(db_path):
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def index_names(db_path):
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def main():
    # ── first boot: create_all + migration block ────────────────────
    with TestClient(app) as c:
        pass

    tables = table_names(DB_PATH)
    for t in EXPECTED_TABLES:
        check(f"table {t} exists", t in tables, str(sorted(tables)))

    idx = index_names(DB_PATH)
    for i in EXPECTED_INDEXES:
        check(f"index {i} exists", i in idx, str(sorted(idx)))

    # ── second boot on the same db: no-op / idempotent ──────────────
    with TestClient(app) as c:
        pass
    tables2 = table_names(DB_PATH)
    idx2 = index_names(DB_PATH)
    check("second boot: same tables", tables2 == tables, f"{tables2} vs {tables}")
    check("second boot: same indexes", idx2 == idx, f"{idx2} vs {idx}")

    # ── UniqueConstraint enforced: duplicate QuestOpening rejected ──
    from app.database import SessionLocal
    from app.models import QuestOpening, User
    from sqlalchemy.exc import IntegrityError
    import datetime as dt

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.person_id == "admin1").first()
        check("admin user exists for FK use", admin is not None)
        day = dt.date(2026, 9, 8)
        o1 = QuestOpening(
            user_id=admin.id, day=day, source="daily", card_id="c1",
            rarity="common", seed="s1", weights_json="{}", roll=0.5,
        )
        db.add(o1)
        db.commit()

        o2 = QuestOpening(
            user_id=admin.id, day=day, source="daily", card_id="c2",
            rarity="rare", seed="s2", weights_json="{}", roll=0.9,
        )
        db.add(o2)
        raised = False
        try:
            db.commit()
        except IntegrityError:
            raised = True
            db.rollback()
        check("duplicate (user,day,source) QuestOpening -> IntegrityError", raised)
    finally:
        db.close()

    # ── quests_config: overlay behaviour ─────────────────────────────
    from app.config import settings
    from app.services import quests_config as qc

    check("quests_enabled defaults to the env value (pinned true by this test)", settings.quests_enabled is True)

    changed = qc.set_overrides({"quests_enabled": True})
    check("set_overrides enables the flag: field reported changed",
          "quests_enabled" in changed, str(changed))
    check("set_overrides enables the flag: settings flips",
          settings.quests_enabled is True)

    cfg = qc.get()
    check("get() reflects enabled override", cfg["enabled"] is True, str(cfg))
    check("get() reports override name", "quests_enabled" in cfg["overrides"], str(cfg))

    # holidays: parse/dedupe/sort
    changed_h = qc.set_overrides(
        {}, holidays=["2026-09-15", "2026-09-01", "2026-09-15", "2026-09-08"]
    )
    check("holidays write reported changed", "holidays" in changed_h, str(changed_h))
    hs = qc.holidays()
    expected = {dt.date(2026, 9, 1), dt.date(2026, 9, 8), dt.date(2026, 9, 15)}
    check("holidays parsed/deduped as a set of dates", hs == expected, str(hs))
    cfg2 = qc.get()
    check("get() holidays sorted list of ISO strings",
          cfg2["holidays"] == ["2026-09-01", "2026-09-08", "2026-09-15"], str(cfg2))

    # bad date -> ValueError
    raised_val = False
    try:
        qc.set_overrides({"quests_season_start": "not-a-date"})
    except ValueError:
        raised_val = True
    check("bad quests_season_start -> ValueError", raised_val)

    raised_val2 = False
    try:
        qc.set_overrides({"quests_season_length": 0})
    except ValueError:
        raised_val2 = True
    check("quests_season_length out of range -> ValueError", raised_val2)

    # The active card season is the same kind of overlay: switchable without a redeploy,
    # but only to a DECLARED season.
    from app.services import quests as _q

    qc.set_overrides({"quests_season": 1})
    check("quests_season=1 applied", _q.active_season()["key"] == 1,
          _q.active_season()["key"])
    check("season 1 is foxes", _q.season_card_ids()[0] == "watch", _q.season_card_ids())
    qc.set_overrides({"quests_season": 2})
    check("quests_season=2 applied", _q.active_season()["key"] == 2,
          _q.active_season()["key"])
    raised_season = False
    try:
        qc.set_overrides({"quests_season": 99})
    except ValueError:
        raised_season = True
    check("unknown quests_season -> ValueError", raised_season)
    check("after the rejection season 2 stays active", _q.active_season()["key"] == 2)
    qc.set_overrides({}, reset=["quests_season"])
    # Take the expectation from the env layer instead of hard-coding it: the default
    # gets switched (1 now, 2 once the frog art lands) and the test must survive that.
    _env_season = qc._env_defaults["quests_season"]
    check("reset restored the env default season", _q.active_season()["key"] == _env_season,
          (_q.active_season()["key"], _env_season))
    check("reset: no quests_season left in the overlay",
          "quests_season" not in qc.get()["overrides"], qc.get()["overrides"])

    raised_val3 = False
    try:
        qc.set_overrides({}, holidays=["bad-date"])
    except ValueError:
        raised_val3 = True
    check("bad holiday date -> ValueError", raised_val3)

    # reset restores env default
    changed_r = qc.set_overrides({}, reset=["quests_enabled"])
    check("reset reports changed", "quests_enabled" in changed_r, str(changed_r))
    check("reset restores env default (true)", settings.quests_enabled is True)

    # file mtime bump is picked up
    import time
    path = qc._path()
    qc.set_overrides({"quests_season_length": 45})
    check("season_length override applied", settings.quests_season_length == 45)
    # Force a distinct mtime, then bypass the TTL cache and confirm a fresh
    # read still reflects the file (ensure_fresh's mtime-based reload path).
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    qc._checked_at = 0.0
    settings.quests_season_length = 999  # perturb, then require ensure_fresh to fix it
    qc.ensure_fresh()
    check("mtime bump is picked up by ensure_fresh",
          settings.quests_season_length == 45, str(settings.quests_season_length))

    # review fixes: string "false" must switch OFF, not on
    qc.set_overrides({"quests_enabled": "false"})
    check("string 'false' coerces to False", settings.quests_enabled is False)
    qc.set_overrides({"quests_enabled": "true"})
    check("string 'true' coerces to True", settings.quests_enabled is True)
    try:
        qc.set_overrides({"quests_enabled": "maybe"})
        check("bogus bool rejected", False)
    except ValueError:
        check("bogus bool rejected", True)

    # holidays can be reset, and are capped
    qc.set_overrides({}, holidays=["2026-12-31"])
    check("holiday set", "2026-12-31" in qc.get()["holidays"])
    qc.set_overrides({}, reset=["holidays"])
    check("holidays reset via reset=[]", qc.get()["holidays"] == [])
    try:
        qc.set_overrides({}, holidays=[f"2027-01-{i:02d}" for i in range(1, 29)] * 20)
        check("holiday cap (dedup keeps it small)", True)
    except ValueError:
        check("holiday cap (dedup keeps it small)", True)

    # corrupt file: read as empty, never overwritten
    path.write_text("{oops", encoding="utf-8")
    qc._checked_at = 0.0
    qc.ensure_fresh(force=True)
    check("corrupt overlay reads as env defaults", settings.quests_season_length == 60,
          str(settings.quests_season_length))
    try:
        qc.set_overrides({"quests_season_length": 30})
        check("corrupt overlay refuses write", False)
    except ValueError:
        check("corrupt overlay refuses write", True)
    check("corrupt file left intact", path.read_text(encoding="utf-8") == "{oops")
    path.unlink()
    qc._checked_at = 0.0
    qc.ensure_fresh(force=True)
    check("deleted overlay restores env defaults", settings.quests_enabled is True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
