"""Quest statistics for the admin page: who opened a surprise, who scored how
much, and "is anyone hooked" — answers the owner's three questions (2026-09-04).

Read-only. All dates are local quest days (`quest_openings.day` and
`quiz_attempts.day` store exactly those), so grouping by day needs no
conversion from UTC. Computed per request; the page is admin-only and
rarely opened, so there is deliberately no cache.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func

from app.i18n import _
from app.models import (
    Employee, QuestCard, QuestCompletion, QuestOpening, QuestProfile,
    QuizAttempt, User,
)
from app.services import quests, perflog

# How many recent working days of the season to show in the per-day feed.
DAYS_SHOWN = 20
# How many working days BEFORE the start to take as the platform attendance baseline
# (the perf log keeps perf_retention_days = 14 days, i.e. ~10 working days).
BASELINE_DAYS = 10
# Before this point the mechanic cannot be judged.
MIN_JUDGE_DAYS = 5

_PAGE_SKIP = ("/api/", "/static/", "/health", "/dev-login", "/login", "/logout", "/auth/", "/s/", "/share/")


def _platform_visitors(day_from: date, day_to: date) -> tuple[dict[date, int], bool]:
    """Unique users with a GET page 200 per local day from the perf log.
    `(counts, sampled)`: sampled=True if at least one record went through
    the sampler with probability < 1 — then this is a lower-bound estimate."""
    tzinfo = quests.tz()
    start = datetime.combine(day_from, time.min, tzinfo=tzinfo).astimezone(timezone.utc)
    end = datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=tzinfo).astimezone(timezone.utc)
    seen: dict[date, set[int]] = defaultdict(set)
    sampled = False
    try:
        for rec in perflog.iter_records(start, end):
            if rec.get("src") != "server" or rec.get("method") != "GET":
                continue
            uid = rec.get("uid")
            if not isinstance(uid, int) or rec.get("status") != 200:
                continue
            path = rec.get("path") or rec.get("route") or ""
            if not isinstance(path, str) or any(path.startswith(x) for x in _PAGE_SKIP):
                continue
            smp = rec.get("sample")
            if isinstance(smp, (int, float)) and smp < 1:
                sampled = True
            local_day = datetime.fromtimestamp(float(rec["ts"]), tz=timezone.utc).astimezone(tzinfo).date()
            seen[local_day].add(uid)
    except Exception:
        return {}, False
    return {d: len(u) for d, u in seen.items()}, sampled


def _season_days(today: date) -> list[date]:
    """Working days from the season start up to and including today (the last DAYS_SHOWN)."""
    start = quests.season_start()
    days: list[date] = []
    cur = start
    for _step in range(4000):
        if cur > today:
            break
        if quests.is_working(cur):
            days.append(cur)
        cur += timedelta(days=1)
    if today not in days and today >= start and not quests.is_working(today):
        days.append(today)  # a day off with openings is shown too
    return days[-DAYS_SHOWN:]


def _source_kind(source: str) -> str:
    if source == "daily":
        return "daily"
    if source == "quiz":
        return "quiz"
    if source.startswith("quest:"):
        return "quest"
    if source.startswith("dup:"):
        return "dup"
    return source


def collect(db) -> dict:
    today = quests.today()
    prev_day = quests.prev_working(today)
    days = _season_days(today)

    # ── profiles and people ─────────────────────────────────────────────
    profiles = db.query(QuestProfile).all()
    user_ids = [p.user_id for p in profiles]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    ovids = [u.person_id for u in users.values() if u.person_id]
    depts: dict[str, str] = {}
    if ovids:
        for ov, dept in db.query(Employee.person_id, Employee.deptname_ru).filter(
                Employee.person_id.in_(ovids)).all():
            if ov and dept and ov not in depts:
                depts[ov] = dept
    active_users = db.query(func.count(User.id)).filter(User.is_active.is_(True)).scalar() or 0

    # ── openings ────────────────────────────────────────────────────────
    openings = db.query(QuestOpening).order_by(QuestOpening.created_at).all()
    by_user_days: dict[int, set[date]] = defaultdict(set)      # days with a daily surprise
    by_user_openings: dict[int, int] = defaultdict(int)
    by_day: dict[date, dict] = defaultdict(lambda: {"daily": 0, "quiz": 0, "quest": 0, "dup": 0,
                                                     "milestone": 0, "users": set(), "pending": 0})
    per_user_day: dict[tuple[int, date], int] = defaultdict(int)
    first_open: dict[int, date] = {}
    today_rows: list[dict] = []
    meta = quests.card_meta()          # translated once, not per opening
    for o in openings:
        kind = _source_kind(o.source)
        by_user_openings[o.user_id] += 1
        per_user_day[(o.user_id, o.day)] += 1
        d = by_day[o.day]
        d[kind] = d.get(kind, 0) + 1
        d["users"].add(o.user_id)
        if o.revealed_at is None:
            d["pending"] += 1
        if o.source == "daily":
            by_user_days[o.user_id].add(o.day)
            if o.user_id not in first_open or o.day < first_open[o.user_id]:
                first_open[o.user_id] = o.day
        if o.day == today:
            u = users.get(o.user_id)
            today_rows.append({
                "person_id": u.person_id if u else str(o.user_id),
                "name": (u.display_name if u else None) or "—",
                "source": o.source,
                "card_id": o.card_id,
                "card": meta.get(o.card_id, {}).get("name", o.card_id),
                "rarity": o.rarity,
                "at": o.created_at,
                "revealed": o.revealed_at is not None,
            })

    # ── quests, quiz, collection ────────────────────────────────────────
    comp_by_user: dict[int, int] = defaultdict(int)
    comp_by_day: dict[date, int] = defaultdict(int)
    comp_by_quest: dict[str, int] = defaultdict(int)
    for c in db.query(QuestCompletion).all():
        comp_by_user[c.user_id] += 1
        comp_by_quest[c.quest_id] += 1
        if c.completed_at is not None:
            # UTC → local quest day
            local_day = (c.completed_at.replace(tzinfo=timezone.utc)
                         .astimezone(quests.tz()).date())
            comp_by_day[local_day] += 1

    quiz_by_user: dict[int, dict] = defaultdict(lambda: {"attempts": 0, "correct": 0, "finished": 0})
    quiz_by_day: dict[date, dict] = defaultdict(lambda: {"started": 0, "finished": 0, "correct": 0})
    for a in db.query(QuizAttempt).all():
        q = quiz_by_user[a.user_id]
        q["attempts"] += 1
        q["correct"] += int(a.correct or 0)
        qd = quiz_by_day[a.day]
        qd["started"] += 1
        if a.finished_at is not None:
            q["finished"] += 1
            qd["finished"] += 1
            qd["correct"] += int(a.correct or 0)

    cards_by_user: dict[int, int] = defaultdict(int)
    for uid, n in db.query(QuestCard.user_id, func.sum(QuestCard.count)).group_by(QuestCard.user_id).all():
        cards_by_user[uid] = int(n or 0)

    # ── "is anyone hooked": retention ───────────────────────────────────
    opened_today = {uid for uid, ds in by_user_days.items() if today in ds}
    opened_prev = {uid for uid, ds in by_user_days.items() if prev_day in ds}
    two_plus = [uid for uid, ds in by_user_days.items() if len(ds) >= 2]
    three_plus = [uid for uid, ds in by_user_days.items() if len(ds) >= 3]
    # Came back on the next working day after their first surprise
    # (counting only those for whom that day has already arrived).
    eligible = 0
    returned = 0
    for uid, d0 in first_open.items():
        nxt = quests.next_working(d0)
        if nxt > today:
            continue
        eligible += 1
        if nxt in by_user_days[uid]:
            returned += 1
    never_opened = [p.user_id for p in profiles if p.user_id not in by_user_days]
    lost = [uid for uid, ds in by_user_days.items()
            if today not in ds and prev_day not in ds and max(ds) < prev_day]

    # ── "are there enough surprises": openings per day per active user ──
    dist = {"1": 0, "2": 0, "3+": 0}
    for (_uid, _d), n in per_user_day.items():
        dist["1" if n == 1 else "2" if n == 2 else "3+"] += 1
    user_days_total = sum(dist.values())
    openings_total = len(openings)

    # ── people table ────────────────────────────────────────────────────
    people = []
    for p in profiles:
        u = users.get(p.user_id)
        ov = u.person_id if u else str(p.user_id)
        ds = by_user_days.get(p.user_id, set())
        q = quiz_by_user.get(p.user_id, {"attempts": 0, "correct": 0, "finished": 0})
        people.append({
            "person_id": ov,
            "name": (u.display_name if u else None) or "—",
            "dept": depts.get(ov, "—"),
            "points": int(p.points or 0),
            "streak": int(p.streak or 0),
            "best_streak": int(p.best_streak or 0),
            "days": len(ds),
            "last_open": max(ds) if ds else None,
            "today": p.user_id in opened_today,
            "openings": by_user_openings.get(p.user_id, 0),
            "quests": comp_by_user.get(p.user_id, 0),
            "quiz_finished": q["finished"],
            "quiz_correct": q["correct"],
            "cards": cards_by_user.get(p.user_id, 0),
            "optin": bool(p.leaderboard_optin),
            "created_at": p.created_at,
        })
    people.sort(key=lambda r: (-r["points"], -r["days"], r["person_id"]))

    # ── per-day feed ────────────────────────────────────────────────────
    timeline = []
    seen_users: set[int] = set()
    for d in sorted(set(days) | {x for x in by_day if x >= (days[0] if days else x)}):
        if days and d < days[0]:
            continue
        row = by_day.get(d, {"daily": 0, "quiz": 0, "quest": 0, "dup": 0, "milestone": 0,
                             "users": set(), "pending": 0})
        new_users = {uid for uid, d0 in first_open.items() if d0 == d}
        seen_users |= new_users
        qd = quiz_by_day.get(d, {"started": 0, "finished": 0, "correct": 0})
        timeline.append({
            "day": d,
            "working": quests.is_working(d),
            "daily": row["daily"],
            "new": len(new_users),
            "quiz_started": qd["started"],
            "quiz_finished": qd["finished"],
            "quests": comp_by_day.get(d, 0),
            "extra": row["quiz"] + row["quest"] + row["dup"] + row.get("milestone", 0),
            "users": len(row["users"]),
        })
    peak_daily = max((t["daily"] for t in timeline), default=0)

    # ── platform attendance: before the season and during it ────────────
    season_start = quests.season_start()
    base_days: list[date] = []
    cur = season_start
    for _step in range(60):
        cur = quests.prev_working(cur)
        base_days.append(cur)
        if len(base_days) >= BASELINE_DAYS:
            break
    base_days.reverse()
    season_days_done = [d for d in days if d < today and quests.is_working(d)]
    visitors, sampled = _platform_visitors(base_days[0] if base_days else season_start, today)
    base_vals = [visitors.get(d, 0) for d in base_days if d in visitors]
    season_vals = [visitors.get(d, 0) for d in season_days_done if d in visitors]
    base_avg = round(sum(base_vals) / len(base_vals), 1) if base_vals else None
    season_avg = round(sum(season_vals) / len(season_vals), 1) if season_vals else None
    visitors_today = visitors.get(today, 0)
    traffic_delta = (round((season_avg - base_avg) / base_avg * 100) if base_avg and season_avg is not None else None)
    daily_by_day = {t["day"]: t["daily"] for t in timeline}
    chart = []
    for d in base_days + [x for x in days]:
        chart.append({"day": d, "season": d >= season_start, "visitors": visitors.get(d),
                      "daily": daily_by_day.get(d, 0), "today": d == today,
                      "working": quests.is_working(d)})
    chart_peak = max([c["visitors"] or 0 for c in chart] + [c["daily"] for c in chart] + [1])

    # ── funnel ──────────────────────────────────────────────────────────
    quest_users = {uid for uid in comp_by_user if comp_by_user[uid] > 0}
    quiz_users = {uid for uid, q in quiz_by_user.items() if q["finished"] > 0}
    opened_any = set(by_user_days)
    funnel = [
        {"label": _("Active platform users"), "n": int(active_users)},
        {"label": _("Visited «Quests»"), "n": len(profiles)},
        {"label": _("Opened the surprise of the day at least once"), "n": len(opened_any)},
        {"label": _("Opened it on 2 or more days"), "n": len(two_plus)},
        {"label": _("Opened it on 3 or more days"), "n": len(three_plus)},
        {"label": _("Completed at least one task"), "n": len(quest_users)},
        {"label": _("Played the quiz to the end"), "n": len(quiz_users)},
    ]
    top = funnel[0]["n"] or 1
    for f in funnel:
        f["pct"] = round(f["n"] / top * 100)

    # "Core": opened on at least 80 % of working days since their first surprise (at least 3 days counted).
    core = 0
    for uid, d0 in first_open.items():
        avail = [d for d in days if d >= d0 and d <= today and quests.is_working(d)]
        if len(avail) < 3:
            continue
        got = len([d for d in avail if d in by_user_days[uid]])
        if got / len(avail) >= 0.8:
            core += 1
    share_today = round(len(opened_today) / len(opened_any) * 100) if opened_any else None
    quiz_correct_total = sum(q["correct"] for q in quiz_by_user.values())
    quiz_finished_total = sum(q["finished"] for q in quiz_by_user.values())
    quiz_accuracy = round(quiz_correct_total / (quiz_finished_total * 3) * 100) if quiz_finished_total else None

    # ── verdict ─────────────────────────────────────────────────────────
    judge_days = len(season_days_done) + (1 if quests.is_working(today) else 0)
    enough = judge_days >= MIN_JUDGE_DAYS
    reach_pct = round(len(opened_any) / active_users * 100) if active_users else 0

    def _grade(ok: bool | None, warn: bool = False) -> str:
        if ok is None:
            return "none"
        return "ok" if ok else ("warn" if warn else "bad")

    q_reach = {
        "q": _("Did the mechanic reach people?"),
        "value": f"{reach_pct} %",
        "line": _("{opened} of {active} active users opened at least one surprise",
                  opened=len(opened_any), active=active_users),
        "grade": _grade(reach_pct >= 30, reach_pct >= 15),
        "rule": _("good from 30%, alarming below 15%"),
    }
    d1 = round(returned / eligible * 100) if eligible else None
    q_habit = {
        "q": _("Do people come back for the surprise?"),
        "value": f"{d1} %" if d1 is not None else "—",
        "line": _("{returned} of {eligible} returned on the next working day after their first surprise · core (≥80% of days): {core} people · today {today}% of those who ever opened one did so",
                  returned=returned, eligible=eligible, core=core,
                  today=share_today if share_today is not None else "—"),
        "grade": _grade(None if d1 is None else d1 >= 50, d1 is not None and d1 >= 30),
        "rule": _("good from 50% return, alarming below 30%"),
    }
    depth_pct = round(len(quest_users | quiz_users) / len(profiles) * 100) if profiles else None
    q_depth = {
        "q": _("Do they do anything besides opening it?"),
        "value": f"{depth_pct} %" if depth_pct is not None else "—",
        "line": _("{quests} people completed the task, {quiz} people played the quiz{accuracy} · an average of {per_day} opens per person per day",
                  quests=len(quest_users), quiz=len(quiz_users),
                  accuracy=(_(" ({pct}% correct answers)", pct=quiz_accuracy)
                            if quiz_accuracy is not None else ""),
                  per_day=round(openings_total / user_days_total, 1)
                  if user_days_total else 0),
        "grade": _grade(None if depth_pct is None else depth_pct >= 40, depth_pct is not None and depth_pct >= 20),
        "rule": _("share of participants; good from 40%, alarming below 20%"),
    }
    if traffic_delta is None:
        t_val, t_grade = "—", "none"
        t_line = _("no performance log data for the base period (telemetry is off or the retention period has expired)")
    else:
        sign = "+" if traffic_delta >= 0 else ""
        t_val = f"{sign}{traffic_delta} %"
        t_line = (_("unique visitors per working day: before the season {base} (over {base_days} days), during the season {season} (over {season_days} days), today {today}",
                    base=base_avg, base_days=len(base_vals), season=season_avg,
                    season_days=len(season_vals), today=visitors_today)
                  + (_(" · sampled estimate") if sampled else ""))
        t_grade = _grade(traffic_delta >= 15, traffic_delta >= 0)
    q_traffic = {
        "q": _("Have people started visiting the platform more often?"),
        "value": t_val, "line": t_line, "grade": t_grade,
        "rule": _("good from +15% over baseline, alarming on a decline"),
    }
    questions = [q_traffic, q_habit, q_reach, q_depth]
    grades = [q["grade"] for q in questions if q["grade"] != "none"]
    if not enough:
        verdict = {
            "kind": "wait",
            "title": _("Too early to tell"),
            "text": _("{done} of the {need} needed working days have passed. The first few days are always better than average: watch return rate and attendance after {date}.",
                      done=judge_days, need=MIN_JUDGE_DAYS,
                      date=quests.working_days_ahead(
                          quests.next_working(today),
                          MIN_JUDGE_DAYS - judge_days)[-1]),
        }
    elif grades and grades.count("bad") >= 2:
        verdict = {"kind": "bad", "title": _("The mechanic isn't working"),
                   "text": _("Two or more signals are below the alarm threshold. Either change the rewards and tasks, or turn it off.")}
    elif grades and "bad" not in grades and grades.count("ok") >= 2:
        verdict = {"kind": "ok", "title": _("It's working, keep it"),
                   "text": _("People come back and do something besides just opening it. Keep an eye on the «core» and on attendance.")}
    else:
        verdict = {"kind": "warn", "title": _("Working at half strength"),
                   "text": _("The signals are mixed: people open the surprise, but there's no depth or growth in attendance. Consider adding tasks or a second reason to come back.")}

    # ADR-17b: how many people have the creature (full collection).
    # Count against the COMPANION SEASON's set and only that: without a
    # card_id filter four foxes and four toads would read as "companion present" here.
    _comp_ids = quests.season_card_ids(quests.COMPANION_SEASON)
    companions = int(db.query(QuestCard.user_id)
                     .filter(QuestCard.count > 0, QuestCard.card_id.in_(_comp_ids))
                     .group_by(QuestCard.user_id)
                     .having(func.count(func.distinct(QuestCard.card_id)) >= len(_comp_ids)).count())
    return {
        "verdict": verdict,
        "questions": questions,
        "funnel": funnel,
        "chart": chart,
        "chart_peak": chart_peak,
        "traffic": {"base_avg": base_avg, "season_avg": season_avg, "today": visitors_today,
                    "delta": traffic_delta, "sampled": sampled,
                    "base_days": len(base_vals), "season_days": len(season_vals)},
        "judge_days": judge_days, "min_judge_days": MIN_JUDGE_DAYS, "core": core,
        "today": today,
        "tz_offset": quests.now().utcoffset() or timedelta(0),
        "prev_day": prev_day,
        "season_start": quests.season_start(),
        "season_day": quests.season_day_index(today) + 1,
        "summary": {
            "active_users": int(active_users),
            "participants": len(profiles),
            "companions": companions,
            "never_opened": len(never_opened),
            "opened_today": len(opened_today),
            "opened_prev": len(opened_prev),
            "two_plus": len(two_plus),
            "three_plus": len(three_plus),
            "returned": returned,
            "eligible": eligible,
            "lost": len(lost),
            "openings_total": openings_total,
            "user_days": user_days_total,
            "avg_per_user_day": round(openings_total / user_days_total, 2) if user_days_total else 0.0,
            "dist": dist,
            "quests_total": sum(comp_by_quest.values()),
            "quiz_finished_total": sum(q["finished"] for q in quiz_by_user.values()),
            "pending_today": by_day.get(today, {}).get("pending", 0) if today in by_day else 0,
        },
        "quests_by_id": [
            {"quest_id": spec["id"], "title": spec.get("title", spec["id"]),
             "done": comp_by_quest.get(spec["id"], 0)}
            for spec in quests.QUESTS
        ],
        "timeline": timeline,
        "peak_daily": peak_daily,
        "today_rows": today_rows,
        "people": people,
    }
