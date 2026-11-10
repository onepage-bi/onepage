"""Smoke test: the streak and the reward track (app/services/quests.py, phase 2).

Pure functions, the app is not started. Holidays go through
`quests._HOLIDAYS_OVERRIDE`.
"""
import os
import sys
import tempfile
from datetime import date, timedelta

TMP = tempfile.mkdtemp(prefix="op-quests-streak-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_CACHE_WARM": "0",
    "OP_QUESTS_ENABLED": "true",
})
sys.path.insert(0, os.getcwd())
from app.services import quests as q  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


D = date
MON, TUE, WED, THU, FRI = D(2026, 9, 7), D(2026, 9, 8), D(2026, 9, 9), D(2026, 9, 10), D(2026, 9, 11)
MON2, TUE2, WED2, THU2, FRI2 = D(2026, 9, 14), D(2026, 9, 15), D(2026, 9, 16), D(2026, 9, 17), D(2026, 9, 18)
MON3, TUE3 = D(2026, 9, 21), D(2026, 9, 22)
W1, W2, W3 = q.iso_week_key(MON), q.iso_week_key(MON2), q.iso_week_key(MON3)


def main():
    q._HOLIDAYS_OVERRIDE = set()
    check("weeks differ", len({W1, W2, W3}) == 3, (W1, W2, W3))

    # (label, last, streak, freeze, day) -> (streak, freeze, event)
    cases = [
        ("first opening",             (None, 0, None, WED),      (1, None, "first")),
        ("same day",                  (WED, 5, W1, WED),         (5, W1, "same_day")),
        ("Thu->Fri in a row",         (THU, 5, None, FRI),       (6, None, "continued")),
        ("Fri->Mon over the weekend", (FRI, 5, None, MON2),      (6, None, "continued")),
        ("Fri->Tue: freeze",          (FRI, 5, None, TUE2),      (6, W2, "frozen")),
        ("Fri->Wed: reset",           (FRI, 5, None, WED2),      (1, None, "reset")),
        ("second freeze in a week",   (TUE2, 6, W2, THU2),       (1, W2, "reset")),
        ("freeze available again",    (FRI2, 6, W2, TUE3),       (7, W3, "frozen")),
        ("date in the past",          (FRI2, 6, None, MON2),     (6, None, "same_day")),
    ]
    for label, args, want in cases:
        got = q.apply_open(*args)
        check(label, got == want, f"got={got} want={want}")

    # A holiday in the middle does not break the streak.
    q._HOLIDAYS_OVERRIDE = {WED}
    got = q.apply_open(TUE, 4, None, THU)
    check("holiday in between: continued", got == (5, None, "continued"), got)
    got = q.apply_open(TUE, 4, None, FRI)
    check("holiday + skipped Thu: freeze", got == (5, q.iso_week_key(FRI), "frozen"), got)
    q._HOLIDAYS_OVERRIDE = set()

    # The freeze is not spent when it was not applied.
    got = q.apply_open(THU, 3, W1, FRI)
    check("continued leaves freeze_week alone", got == (4, W1, "continued"), got)
    got = q.apply_open(FRI, 9, W1, WED2)
    check("reset leaves freeze_week alone", got == (1, W1, "reset"), got)

    # ── milestones ───────────────────────────────────────────────
    for n, rare, extra in [(1, False, False), (3, True, False), (6, True, False),
                           (7, False, True), (9, True, False), (21, True, True)]:
        m = q.milestones(n)
        check(f"milestones({n})", m == {"rare_x2": rare, "extra_surprise": extra}, m)

    # ── reward track ─────────────────────────────────────────────
    q._HOLIDAYS_OVERRIDE = {WED2}
    path = q.reward_path(MON2, 4, opened_today=True)
    check("track has 7 steps", len(path) == 7, len(path))
    check("today is marked today", path[0]["today"] and not any(p["today"] for p in path[1:]))
    check("opened today -> done", path[0]["done"] and not any(p["done"] for p in path[1:]))
    check("opened today -> streak_n = current", path[0]["streak_n"] == 4, path[0]["streak_n"])
    check("streak_n grows by 1 per step", [p["streak_n"] for p in path] == [4, 5, 6, 7, 8, 9, 10],
          [p["streak_n"] for p in path])
    check("offset 0..6", [p["offset"] for p in path] == list(range(7)))
    check("track has working days only", all(q.is_working(p["day"]) for p in path),
          [str(p["day"]) for p in path])
    check("holiday skipped", all(p["day"] != WED2 for p in path), [str(p["day"]) for p in path])
    check("milestone flags match milestones()",
          all(p["rare_x2"] == q.milestones(p["streak_n"])["rare_x2"]
              and p["extra"] == q.milestones(p["streak_n"])["extra_surprise"] for p in path))

    path2 = q.reward_path(MON2, 4, opened_today=False)
    check("not opened today -> done nowhere", not any(p["done"] for p in path2))
    check("not opened today -> streak_n from the next one", [p["streak_n"] for p in path2] == [5, 6, 7, 8, 9, 10, 11],
          [p["streak_n"] for p in path2])
    check("same days regardless of opened_today",
          [p["day"] for p in path] == [p["day"] for p in path2])
    q._HOLIDAYS_OVERRIDE = set()

    path3 = q.reward_path(D(2026, 9, 12), 0, opened_today=False)  # Saturday
    check("from a weekend the track starts on a working day", path3[0]["day"] == MON2, path3[0]["day"])
    check("zero streak -> first step is 1", path3[0]["streak_n"] == 1, path3[0]["streak_n"])
    check("on a weekend no step is 'today'", not any(p["today"] for p in path3))
    path4 = q.reward_path(D(2026, 9, 12), 4, opened_today=True)  # Saturday, "opened" is ignored
    check("on a weekend done nowhere and the streak counts from the next one", not any(p["done"] for p in path4) and path4[0]["streak_n"] == 5,
          (path4[0]["done"], path4[0]["streak_n"]))

    # review fixes: weekend/holiday opens never reset; freeze charged to the missed day's week;
    # rewinding the clock is harmless
    fri, sat, sun, mon = date(2026, 9, 4), date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)
    check("open on Saturday keeps streak", q.apply_open(fri, 20, None, sat) == (20, None, "same_day"))
    check("open on Sunday keeps streak", q.apply_open(fri, 20, None, sun) == (20, None, "same_day"))
    check("Sat then Mon continues", q.apply_open(sat, 20, None, mon) == (21, None, "continued"))
    check("past date is a no-op", q.apply_open(mon, 20, None, fri) == (20, None, "same_day"))
    # Fri W36 missed → open Tue W37: freeze charged to W37 (week of the missed Monday)
    tue = date(2026, 9, 8)
    ns, fw, ev = q.apply_open(fri, 5, None, tue)
    check("freeze charged to missed day's week", (ns, ev, fw) == (6, "frozen", q.iso_week_key(mon)), str((ns, fw, ev)))
    # freeze already spent in W37 → second miss in W37 resets
    thu = date(2026, 9, 10)
    check("second miss same week resets", q.apply_open(tue, 6, fw, thu)[2] == "reset")
    # holiday-only calendar guard
    q._HOLIDAYS_OVERRIDE = {date(2026, 1, 1) + timedelta(days=i) for i in range(4000)}
    try:
        q.next_working(date(2026, 1, 1)); check("pathological calendar raises", False)
    except ValueError:
        check("pathological calendar raises", True)
    q._HOLIDAYS_OVERRIDE = set()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
