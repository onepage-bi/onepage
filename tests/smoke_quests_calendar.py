"""Smoke test: the quests calendar (app/services/quests.py, phase 2).

The app is NOT started: only `app.services.quests` is imported
(and `app.config` right after it, so env is set up as in the other
smoke tests). Holidays are injected via `quests._HOLIDAYS_OVERRIDE`:
the reference table does not exist at this stage yet.
"""
import os
import sys
import tempfile
from datetime import date

TMP = tempfile.mkdtemp(prefix="op-quests-cal-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_CACHE_WARM": "0",
    "OP_QUESTS_ENABLED": "true",
})
sys.path.insert(0, os.getcwd())
from app.config import settings  # noqa: E402
from app.services import quests as q  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# Reference week: 2026-09-07 Monday … 2026-09-13 Sunday.
MON = date(2026, 9, 7)
TUE = date(2026, 9, 8)
WED = date(2026, 9, 9)
THU = date(2026, 9, 10)
FRI = date(2026, 9, 11)
SAT = date(2026, 9, 12)
SUN = date(2026, 9, 13)
NEXT_MON = date(2026, 9, 14)
NEXT_TUE = date(2026, 9, 15)


def main():
    check("reference dates are that very week",
          [d.weekday() for d in (MON, TUE, WED, THU, FRI, SAT, SUN)] == [0, 1, 2, 3, 4, 5, 6])

    # ── weekends ─────────────────────────────────────────────────
    q._HOLIDAYS_OVERRIDE = set()
    check("weekdays are working", all(q.is_working(d) for d in (MON, TUE, WED, THU, FRI)))
    check("Saturday is non-working", not q.is_working(SAT))
    check("Sunday is non-working", not q.is_working(SUN))
    check("next_working(Fri) = Mon", q.next_working(FRI) == NEXT_MON, q.next_working(FRI))
    check("next_working(Sat) = Mon", q.next_working(SAT) == NEXT_MON, q.next_working(SAT))
    check("prev_working(Mon) = previous Fri", q.prev_working(MON) == date(2026, 9, 4), q.prev_working(MON))
    check("prev_working is strictly earlier", q.prev_working(FRI) == THU, q.prev_working(FRI))
    check("next_working is strictly later", q.next_working(THU) == FRI, q.next_working(THU))

    # ── holidays ─────────────────────────────────────────────────
    q._HOLIDAYS_OVERRIDE = {WED}
    check("holiday Wednesday is non-working", not q.is_working(WED))
    check("next_working(Tue) skipped the holiday", q.next_working(TUE) == THU, q.next_working(TUE))
    check("prev_working(Thu) skipped the holiday", q.prev_working(THU) == TUE, q.prev_working(THU))
    check("working_days_between(Tue,Thu) with a holiday = 1",
          q.working_days_between(TUE, THU) == 1, q.working_days_between(TUE, THU))
    q._HOLIDAYS_OVERRIDE = set()

    # ── working_days_between ─────────────────────────────────────
    check("Fri→Mon = 1", q.working_days_between(FRI, NEXT_MON) == 1, q.working_days_between(FRI, NEXT_MON))
    check("Fri→Tue = 2", q.working_days_between(FRI, NEXT_TUE) == 2, q.working_days_between(FRI, NEXT_TUE))
    check("same day = 0", q.working_days_between(FRI, FRI) == 0, q.working_days_between(FRI, FRI))
    check("reversed = 0", q.working_days_between(NEXT_MON, FRI) == 0, q.working_days_between(NEXT_MON, FRI))
    check("Mon→Fri = 4", q.working_days_between(MON, FRI) == 4, q.working_days_between(MON, FRI))
    check("Fri->Sun = 0 (weekend only)", q.working_days_between(FRI, SUN) == 0, q.working_days_between(FRI, SUN))

    # ── working_days_ahead ───────────────────────────────────────
    q._HOLIDAYS_OVERRIDE = {WED}
    ahead = q.working_days_ahead(TUE, 7)
    check("working_days_ahead returned 7 dates", len(ahead) == 7, len(ahead))
    check("working_days_ahead starts with the day itself", ahead[0] == TUE, ahead[0])
    check("no weekends or holidays on the track", all(q.is_working(d) for d in ahead), ahead)
    check("track is strictly increasing", all(b > a for a, b in zip(ahead, ahead[1:])), ahead)
    check("track skipped the holiday Wednesday", WED not in ahead, ahead)
    from_weekend = q.working_days_ahead(SAT, 7)
    check("from a weekend the track starts on Monday", from_weekend[0] == NEXT_MON, from_weekend[0])
    q._HOLIDAYS_OVERRIDE = set()

    # ── season and quiz bank ─────────────────────────────────────
    old_start = settings.quests_season_start
    settings.quests_season_start = MON.isoformat()
    check("season_start read", q.season_start() == MON, q.season_start())
    check("start day index = 0", q.season_day_index(MON) == 0, q.season_day_index(MON))
    check("index before the start clamps to 0", q.season_day_index(date(2026, 8, 1)) == 0)
    check("weekend index = Friday index", q.season_day_index(SAT) == q.season_day_index(FRI))
    day15 = date(2026, 9, 25)   # Friday of week three, the 15th working day
    day16 = date(2026, 9, 28)   # the following Monday
    check("15th working day has index 14", q.season_day_index(day15) == 14, q.season_day_index(day15))
    check("day 15 -> bank 15", q.quiz_bank_day(day15) == 15, q.quiz_bank_day(day15))
    check("day 16 -> bank 1", q.quiz_bank_day(day16) == 1, q.quiz_bank_day(day16))
    check("day 1 -> bank 1", q.quiz_bank_day(MON) == 1, q.quiz_bank_day(MON))
    check("bank always 1..15",
          all(1 <= q.quiz_bank_day(MON + __import__("datetime").timedelta(days=i)) <= 15 for i in range(70)))
    settings.quests_season_start = "not-a-date"
    check("broken season_start -> today", q.season_start() == q.today(), q.season_start())
    settings.quests_season_start = old_start

    # ── ISO weeks ────────────────────────────────────────────────
    for d in (date(2026, 12, 28), date(2027, 1, 4), date(2026, 1, 1), MON):
        y, w, _ = d.isocalendar()
        check(f"iso_week_key({d})", q.iso_week_key(d) == f"{y}-W{w:02d}", q.iso_week_key(d))
    check("2026-12-28 lands in the next year's week",
          q.iso_week_key(date(2026, 12, 28)).startswith("2026-W53")
          or q.iso_week_key(date(2026, 12, 28)).startswith("2027-W01"),
          q.iso_week_key(date(2026, 12, 28)))
    check("2027-01-04 → 2027-W01", q.iso_week_key(date(2027, 1, 4)) == "2027-W01",
          q.iso_week_key(date(2027, 1, 4)))
    check("Mon and Fri share the week key", q.iso_week_key(MON) == q.iso_week_key(FRI))
    check("Fri and next Mon have different week keys",
          q.iso_week_key(FRI) != q.iso_week_key(NEXT_MON))

    # ── time zone ────────────────────────────────────────────────
    old_tz = settings.quests_tz
    settings.quests_tz = "Asia/Almaty"
    q._tz_cache = None
    off_ok = q.now().utcoffset()
    check("Almaty = UTC+5", off_ok is not None and off_ok.total_seconds() == 5 * 3600, off_ok)
    settings.quests_tz = "Nowhere/Nothing"
    q._tz_cache = None
    zone = q.tz()
    check("garbage zone -> fallback UTC+5", zone is q._FALLBACK_TZ, zone)
    check("now() on fallback is still aware +5",
          q.now().utcoffset().total_seconds() == 5 * 3600, q.now().utcoffset())
    check("today() returns a date", isinstance(q.today(), date))
    settings.quests_tz = old_tz
    q._tz_cache = None

    # ── date.today() must not appear in the module ───────────────
    src = open(q.__file__, encoding="utf-8").read()
    check("no date.today( in the module", "date.today(" not in src)
    check("no zone-less datetime.now() in the module", "datetime.now()" not in src)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
