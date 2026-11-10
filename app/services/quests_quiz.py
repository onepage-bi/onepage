"""Quests — the daily quiz engine: question bank, session, scoring.

A separate module next to `quests.py`: that one is a pure DB-free core (calendar,
streak, cards), this one is everything that touches the `quiz_questions` and
`quiz_attempts` tables. The routes (phase 5) only call functions from here and commit.

Four decisions worth keeping in mind while reading:

* **The server keeps time, not the browser.** The client sends neither `elapsed`
  nor "I made it": for each question the server writes `served_at` into
  `answers_json` at issue time and measures the difference itself in `answer()`.
  Otherwise the stopwatch lives in JS and can be edited in the console. Two
  seconds of network latency are forgiven explicitly (`+2 s`) so that an honest
  answer "on the last second" is not lost to ping.

* **`begin()` sets the stamp, not `start()`.** `start()` only creates (or
  finds) the attempt and returns the question — the timer does NOT start: an
  intro screen sits between the server's response and the moment the person
  actually sees the question, and the seconds spent on it would be stolen. A
  separate `begin(token)` idempotently sets `served_at` on the current question
  and returns the honest remainder; a repeated `begin` does not extend the time.
  Resuming (`start()` on an already issued question) first expires
  overdue questions automatically (`pick=None`, `timeout=True`) — otherwise
  a closed tab would freeze the timer forever.

* **Answers do not leave the server early.** `public_question()` is the
  only way to hand a question out, and its dict physically contains
  neither `answer_idx` nor `explanation`. The correct answer and the explanation
  are returned only in the response to a choice already made.

* **One attempt per working day.** The guarantee is not a check "has he
  played already" but UNIQUE `(user_id, day)`: the insert runs in `begin_nested()`,
  and on IntegrityError we re-read the other (our own, from a sibling worker)
  row. An unfinished attempt is not recreated on a repeated `start()`
  but continued — and, more importantly, the current question's `served_at`
  is NOT refreshed then: reloading the page must not gift extra time.

* **The opponent is yesterday's colleagues.** "Colleagues yesterday" =
  `round(avg(correct))` over finished attempts for `prev_working(today)`,
  falling back to 2 if nobody played yesterday. The value is cached for 60 seconds:
  it is the same for everyone and barely changes within a day.
"""
from __future__ import annotations

import json
import math
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import i18n
from app.i18n import _
from app.models import AuditLog, QuizAttempt, QuizQuestion
from app.services import quests as quests_mod  # noqa: E402
from app.services.quests import is_working, level, now, prev_working, quiz_bank_day, today  # noqa: F401

# ── Errors ───────────────────────────────────────────────────────────────


class QuizError(Exception):
    """Base error of the quiz engine."""


class AlreadyPlayed(QuizError):
    """Today's attempt is already finished."""


class QuizNotFound(QuizError):
    """No such attempt for this user (or someone else's token)."""


class QuizStale(QuizError):
    """Answer to an already answered question — a second tab or a double submit."""


class QuizClosed(QuizError):
    """Day off or holiday: the quiz runs on working days only."""


class QuizUnavailable(QuizError):
    """The bank has no active questions for this day."""


# ── Common ───────────────────────────────────────────────────────────────

# Languages a question MAY be translated into: everything the interface knows except
# Russian — the Russian text lives in the question's own columns and serves as the fallback.
TRANSLATABLE = tuple(l for l in i18n.LANGS if l != i18n.DEFAULT)

# Length headroom for a translation. English in this bank runs about 15 % longer
# than the Russian original (longest answer option: 222 characters vs 194), and
# it can legitimately hit the Russian ceiling without any padding. One and a half
# ceilings is the limit beyond which an option no longer fits the card in
# any language, i.e. the problem is not the translation but the question.
TR_SLACK = 1.5

TOTAL = 3           # questions per daily attempt
GRACE_SEC = 2       # network latency allowance
DEFAULT_SEC = 60    # if the bank does not specify `sec`
# How many days the bank holds is computed from the bank itself, not set as a number.
# There used to be a hard `BANK_DAYS = 15` here, and the validator demanded exactly 45
# questions with days 1..15: the bank was IMPOSSIBLE to extend, however good
# the new questions were. Hence the complaint "the same questions keep coming" —
# adding more was impossible by construction. The lower bound stayed: less than
# one full day is not a bank.
BANK_DAYS_MIN = 15          # do not let a short file truncate the bank by accident
PER_DAY = 3


def bank_days_of(bank: list) -> int:
    """How many days the given bank describes. Its length must divide by PER_DAY.

    On an empty or too short bank it honestly returns 0, not the lower
    bound: the function answers "how many days are here", and substituting
    a placeholder would hide an empty bank from whoever is asking.
    Such a bank is rejected by the check in `import_bank`, not by this line.
    """
    try:
        return max(0, len(bank)) // PER_DAY
    except TypeError:
        return 0

# Caps on bank import. Their job is not "pretty formatting" but keeping an
# admin paste from spilling out: `q`/`o` go into the question card's layout,
# `e` into the explanation, and `id` into a PRIMARY KEY that later sits in
# every attempt's `q_ids_json`.
MAX_Q = 400
MAX_OPT = 200
MAX_EXPL = 1000
MAX_TOPIC = 64
MAX_QID = 10 ** 6

_BANK_PATH = Path(__file__).resolve().parent / "data" / "quiz_bank.json"


def _utcnow() -> datetime:
    """Naive UTC — like everything else in the project's database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _log_id(db: Session, actor_id: int | None, action: str, resource_type: str,
            resource_id: str, detail: dict, ip: str = "") -> None:
    """The `_log` canon from `app/api/favorites.py:21-28`, but by id rather than by
    a `User` object: what arrives here is a `QuestProfile`, which only has `user_id`,
    and there is no point loading a whole user row just for the audit."""
    entry = AuditLog(
        actor_id=actor_id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


# ── Bank import ──────────────────────────────────────────────────────────


def _check_translation(err, key: dict, lang: str, tr, options) -> None:
    """One language of one question. `options` are the Russian options: a translation
    must keep the same FOUR in the same order, because `a` (the index of the correct
    answer) is shared by all languages. Reordered options would turn the correct
    answer wrong for exactly those who do not read Russian."""
    field = f"i18n.{lang}"
    if lang == i18n.DEFAULT:
        err(key, field, _("the default language is the question itself; no separate translation is needed"))
        return
    if lang not in TRANSLATABLE:
        err(key, field, _("language {lang} is not supported; available: {langs}",
                          lang=lang, langs=", ".join(TRANSLATABLE)))
        return
    if not isinstance(tr, dict):
        err(key, field, _("a translation must be an object"))
        return
    max_q, max_opt, max_expl = (int(MAX_Q * TR_SLACK), int(MAX_OPT * TR_SLACK),
                                int(MAX_EXPL * TR_SLACK))
    q = tr.get("q")
    if not isinstance(q, str) or not q.strip():
        err(key, field + ".q", _("the question text is empty"))
    elif len(q) > max_q:
        err(key, field + ".q", _("the question text is longer than {max} characters", max=max_q))
    e = tr.get("e")
    if not isinstance(e, str) or not e.strip():
        err(key, field + ".e", _("the explanation is empty"))
    elif len(e) > max_expl:
        err(key, field + ".e", _("the explanation is longer than {max} characters", max=max_expl))
    t = tr.get("t")
    if t is not None and (not isinstance(t, str) or len(t) > MAX_TOPIC):
        err(key, field + ".t", _("the topic must be a string no longer than {max} characters",
                                 max=MAX_TOPIC))
    o = tr.get("o")
    want = len(options) if isinstance(options, list) else 4
    if not isinstance(o, list) or len(o) != want:
        err(key, field + ".o",
            _("exactly {want} answer options are required, as in the original", want=want))
    elif any(not isinstance(x, str) or not x.strip() for x in o):
        err(key, field + ".o", _("answer options cannot be empty"))
    elif any(len(x) > max_opt for x in o):
        err(key, field + ".o", _("an answer option is longer than {max} characters", max=max_opt))


def import_bank(db: Session, bank: list[dict], *, dry_run: bool) -> dict:
    """Validate and (unless `dry_run`) load the question bank.

    Errors are not raised but collected as a list of `{"id"/"index", "field",
    "reason"}` — the admin page shows them all at once, not one per
    run. Writes are all-or-nothing: one bad row cancels the import.
    """
    errors: list[dict] = []

    def err(item_key: dict, field: str, reason: str) -> None:
        row = dict(item_key)
        row["field"] = field
        row["reason"] = reason
        errors.append(row)

    if not isinstance(bank, list):
        return {"ok": False, "count": 0, "days": BANK_DAYS_MIN,
                "errors": [{"index": 0, "field": "bank", "reason": _("the bank must be a list")}],
                "dry_run": dry_run}

    bank_days = bank_days_of(bank)
    if len(bank) % PER_DAY or bank_days < BANK_DAYS_MIN:
        errors.append({"index": -1, "field": "bank",
                       "reason": _("the bank needs {want} questions per day and at least {min_days} days; got {got} questions",
                                   want=PER_DAY, min_days=BANK_DAYS_MIN, got=len(bank))})

    seen_ids: set[int] = set()
    per_day: dict[int, int] = {}

    for i, raw in enumerate(bank):
        key = {"index": i}
        if not isinstance(raw, dict):
            err(key, "item", _("a bank item must be an object"))
            continue

        # NaN/Inf arrive from JSON ("NaN", "Infinity" — an extension
        # `json.loads` accepts by default) and silently break the scoring
        # arithmetic, so they are cut off by a separate check:
        # `isinstance(x, int)` does not catch them, they are floats.
        for _fld in ("id", "day", "a", "sec"):
            _v = raw.get(_fld)
            if isinstance(_v, float) and not math.isfinite(_v):
                err(key, _fld, _("the number must be finite"))

        qid = raw.get("id")
        if isinstance(qid, bool) or not isinstance(qid, int):
            err(key, "id", _("id must be an integer"))
        elif not (0 < qid <= MAX_QID):
            key = {"id": qid, "index": i}
            err(key, "id", _("id must be between 1 and {max}", max=MAX_QID))
        else:
            key = {"id": qid, "index": i}
            if qid in seen_ids:
                err(key, "id", _("duplicate id {id}", id=qid))
            seen_ids.add(qid)

        day = raw.get("day")
        if isinstance(day, bool) or not isinstance(day, int) or not (1 <= day <= bank_days):
            err(key, "day", _("day must be an integer between 1 and {max}", max=bank_days))
        else:
            per_day[day] = per_day.get(day, 0) + 1

        q = raw.get("q")
        if not isinstance(q, str) or not q.strip():
            err(key, "q", _("the question text is empty"))
        elif len(q) > MAX_Q:
            err(key, "q", _("the question text is longer than {max} characters", max=MAX_Q))

        o = raw.get("o")
        if not isinstance(o, list) or len(o) != 4:
            err(key, "o", _("exactly 4 answer options are required"))
        elif any(not isinstance(x, str) or not x.strip() for x in o):
            err(key, "o", _("answer options cannot be empty"))
        elif any(len(x) > MAX_OPT for x in o):
            err(key, "o", _("an answer option is longer than {max} characters", max=MAX_OPT))
        elif len({x.strip().lower() for x in o}) != 4:
            err(key, "o", _("answer options must be distinct"))

        a = raw.get("a")
        if isinstance(a, bool) or not isinstance(a, int) or not (0 <= a <= 3):
            err(key, "a", _("the correct-answer index must be between 0 and 3"))

        e = raw.get("e")
        if not isinstance(e, str):
            err(key, "e", _("the explanation must be a string"))
        elif len(e) > MAX_EXPL:
            err(key, "e", _("the explanation is longer than {max} characters", max=MAX_EXPL))

        sec = raw.get("sec", DEFAULT_SEC)
        if sec is None:
            sec = DEFAULT_SEC
        if isinstance(sec, bool) or not isinstance(sec, int) or not (30 <= sec <= 180):
            err(key, "sec", _("time per question must be an integer between 30 and 180 seconds"))

        t = raw.get("t")
        if not isinstance(t, str) or len(t) > MAX_TOPIC:
            err(key, "t", _("the topic must be a string no longer than {max} characters",
                            max=MAX_TOPIC))

        # Translations. Optional as a whole and per language: a bank without `i18n`
        # is exactly the bank that existed before this version, and it is valid. But if
        # the block is present it is checked as strictly as the Russian original: half
        # a translation on screen is worse than honest Russian.
        tr = raw.get("i18n")
        if tr is not None:
            if not isinstance(tr, dict):
                err(key, "i18n", _("translations must be an object {{language: translation}}"))
            else:
                for lang in sorted(tr):
                    _check_translation(err, key, lang, tr[lang], o)

    for d in range(1, bank_days + 1):
        got = per_day.get(d, 0)
        if got != PER_DAY:
            errors.append({"index": -1, "field": "day",
                           "reason": _("day {day} should have {want} questions, found {got}",
                                       day=d, want=PER_DAY, got=got)})

    # Translation coverage — the reason to run a dry run even on a
    # known-valid bank: how many questions a person will see in their own
    # language and how many in Russian.
    def _tr_of(raw) -> dict:
        # Counted over the RAW bank, which at this point may still be any
        # shape at all — the errors above say so, they do not stop the count.
        tr = raw.get("i18n") if isinstance(raw, dict) else None
        return tr if isinstance(tr, dict) else {}

    coverage = {lang: sum(1 for raw in bank if isinstance(_tr_of(raw).get(lang), dict))
                for lang in TRANSLATABLE}

    ok = not errors
    result = {"ok": ok, "count": len(bank) if ok else 0, "days": bank_days,
              "errors": errors, "dry_run": dry_run,
              "i18n_coverage": coverage,
              "i18n_line": ", ".join(
                  _("{lang}: {n} of {total}", lang=lang, n=n, total=len(bank))
                  for lang, n in coverage.items())}
    if not ok or dry_run:
        if not ok:
            result["count"] = 0
        return result

    # Old questions are retired, not deleted: the `q_ids_json` of attempts
    # already played refer to them.
    db.query(QuizQuestion).update({QuizQuestion.active: False}, synchronize_session=False)
    stamp = _utcnow()
    for raw in bank:
        sec = raw.get("sec", DEFAULT_SEC)
        if sec is None:
            sec = DEFAULT_SEC
        q = QuizQuestion(
            id=int(raw["id"]),
            day=int(raw["day"]),
            topic=str(raw.get("t") or ""),
            question=str(raw["q"]),
            answer_idx=int(raw["a"]),
            explanation=str(raw.get("e") or ""),
            sec=int(sec),
            active=True,
            imported_at=stamp,
        )
        q.set_options(list(raw["o"]))
        # Only known languages and only known fields: the bank is a file from the
        # repository, but the same path accepts JSON uploaded by an admin.
        tr = raw.get("i18n")
        q.set_i18n({
            lang: {k: v for k, v in (tr[lang] or {}).items() if k in ("t", "q", "o", "e")}
            for lang in (sorted(tr) if isinstance(tr, dict) else ())
            if lang in TRANSLATABLE and isinstance(tr.get(lang), dict)
        })
        db.merge(q)
    db.flush()
    result["count"] = len(bank)
    return result


def load_default_bank() -> list[dict]:
    """The bank from the repository (`app/services/data/quiz_bank.json`)."""
    with open(_BANK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def seed_default_bank(db: Session) -> int:
    """Load the bank from the repository if there are no active questions yet.

    Called lazily — on the first quiz start and when the admin page opens —
    to keep the import out of the migrations. Returns the number of questions loaded,
    or 0 if the bank already exists (or the file is broken)."""
    have = db.query(func.count(QuizQuestion.id)).filter(QuizQuestion.active.is_(True)).scalar() or 0
    if have:
        return 0
    try:
        bank = load_default_bank()
    except (OSError, ValueError):
        return 0
    res = import_bank(db, bank, dry_run=False)
    return int(res.get("count") or 0) if res.get("ok") else 0


def bank_status(db: Session) -> dict:
    """What the bank currently holds — for the admin page."""
    rows = db.query(QuizQuestion.day, func.count(QuizQuestion.id)).filter(
        QuizQuestion.active.is_(True)).group_by(QuizQuestion.day).all()
    per_day = {int(d): int(n) for d, n in rows}
    active = sum(per_day.values())
    last = db.query(func.max(QuizQuestion.imported_at)).filter(
        QuizQuestion.active.is_(True)).scalar()
    # As many days as the bank itself has: the admin page shows the state of
    # what exists, not conformance to a long-removed constant.
    days = max(per_day) if per_day else 0
    return {
        "active": active,
        "days": days,
        "days_complete": bool(days) and all(per_day.get(d, 0) >= PER_DAY for d in range(1, days + 1)),
        "last_imported_at": last.isoformat() if isinstance(last, datetime) else last,
    }


# ── Questions ────────────────────────────────────────────────────────────


def bank_cycle(db: Session) -> int:
    """How many DISTINCT days the bank has — that is how long the cycle runs.

    Counted over active questions: a day whose questions were taken off air
    does not lengthen the cycle. Extend the bank with days 16–30 and the cycle
    becomes thirty days by itself, with no code touched.
    """
    from sqlalchemy import func as _f

    n = db.query(_f.max(QuizQuestion.day)).filter(QuizQuestion.active.is_(True)).scalar()
    return int(n) if n else quests_mod.QUIZ_CYCLE_DEFAULT


def day_of(db: Session, d: date) -> int:
    """The bank day for a date — with the cycle length taken from the bank itself."""
    return quiz_bank_day(d, bank_cycle(db))


def _questions_for_day(db: Session, bank_day: int) -> list[QuizQuestion]:
    rows = db.query(QuizQuestion).filter(
        QuizQuestion.active.is_(True), QuizQuestion.day == int(bank_day),
    ).order_by(QuizQuestion.id).all()
    if not rows:
        raise QuizUnavailable(_("no active questions for day {day}", day=bank_day))
    return rows[:TOTAL]


def public_question(q: QuizQuestion, index: int) -> dict:
    """A question for the client. Never `answer_idx`, never `explanation`.

    The language comes from the request (`i18n.current_lang()`), not a parameter: this
    call sits in four places in the engine, and an extra parameter in each would mean
    four chances to forget to pass it. No translation — the Russian original goes out.
    """
    loc = q.localized(i18n.current_lang())
    return {
        "index": index,
        "id": q.id,
        "topic": loc["t"],
        "q": loc["q"],
        "o": loc["o"],
        "sec": q.sec,
    }


def _q_by_id(db: Session, qid: int) -> QuizQuestion | None:
    return db.query(QuizQuestion).filter(QuizQuestion.id == int(qid)).first()


# ── Opponent ─────────────────────────────────────────────────────────────

_OPP_TTL = 60.0
_OPP_CACHE_MAX = 8   # days in the cache: today plus a handful of neighbours, not history
_opp_cache: dict[date, tuple[int, float]] = {}


def opponent(db: Session, d: date) -> int:
    """Colleagues yesterday: the average result for the previous working day."""
    hit = _opp_cache.get(d)
    if hit and (time.time() - hit[1]) < _OPP_TTL:
        return hit[0]
    prev = prev_working(d)
    avg = db.query(func.avg(QuizAttempt.correct)).filter(
        QuizAttempt.day == prev, QuizAttempt.finished_at.isnot(None),
    ).scalar()
    value = 2 if avg is None else int(round(float(avg)))
    value = max(0, min(TOTAL, value))
    _opp_cache[d] = (value, time.time())
    # The key is a calendar day, and without a cap the dict would grow by one entry
    # per day of the process's life. Insertion order = day order, so
    # "the oldest" is simply the first key.
    while len(_opp_cache) > _OPP_CACHE_MAX:
        _opp_cache.pop(next(iter(_opp_cache)))
    return value


# ── Session ──────────────────────────────────────────────────────────────


def _attempt_for(db: Session, user_id: int, d: date) -> QuizAttempt | None:
    return db.query(QuizAttempt).filter(
        QuizAttempt.user_id == user_id, QuizAttempt.day == d,
    ).first()


def _score(attempt: QuizAttempt) -> dict:
    answers = attempt.get_answers()
    return {"correct": sum(1 for a in answers if a.get("correct")),
            "points": sum(int(a.get("pts") or 0) for a in answers)}


def _remaining(question: QuizQuestion, served: datetime | None) -> int | None:
    """Seconds left on the question. `None` — the timer has not started yet."""
    if served is None:
        return None
    elapsed = (_utcnow() - served).total_seconds()
    if elapsed < 0:
        elapsed = 0.0
    # Round UP: `_iso` writes the stamp without microseconds, so right
    # after `begin()` up to a second has already "passed" — rounding down, the client
    # would get 89 of 90 on an honestly zero elapsed time.
    return max(0, math.ceil(int(question.sec) - elapsed))


def _expired(question: QuizQuestion, served: datetime | None) -> bool:
    if served is None:
        return False
    elapsed = (_utcnow() - served).total_seconds()
    return elapsed > (int(question.sec) + GRACE_SEC)


def _finish(db: Session, profile, attempt: QuizAttempt, *, ip: str = "") -> dict:
    """Close the attempt: score to the profile, a card for the quiz, an audit row.

    Called from two places — from `answer()` on the last answer and from
    `start()` when the resume's auto-timeout finished all remaining
    questions. That is why it lives in its own function: a divergence between these two
    paths would mean "attempt closed, but points not credited".
    """
    sc = _score(attempt)
    attempt.finished_at = _utcnow()
    attempt.correct = sc["correct"]
    attempt.points = sc["points"]
    profile.points = int(profile.points or 0) + attempt.points
    profile.quiz_correct_total = int(profile.quiz_correct_total or 0) + attempt.correct

    opening = None
    try:
        from app.services.quests import grant_opening  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        grant_opening = None  # type: ignore[assignment]
    if grant_opening is not None:
        try:
            res = grant_opening(db, profile.user_id, attempt.day, "quiz",
                                int(profile.streak or 0))
        except QuizError:
            raise
        except Exception:
            res = None
        # `grant_opening` returns a `QuestOpening` row (or `None` if an
        # opening from this source already exists for the day) — only what
        # the opening scene needs is handed out.
        if res is not None:
            opening = {
                "card_id": getattr(res, "card_id", None),
                "rarity": getattr(res, "rarity", None),
                "source": getattr(res, "source", None) or "quiz",
            }

    _log_id(db, profile.user_id, "quests_quiz_finish", "quiz_attempt", str(attempt.id),
            {"correct": attempt.correct, "points": attempt.points,
             "bank_day": attempt.bank_day}, ip=ip)
    db.flush()
    return {
        "correct": attempt.correct,
        "points": attempt.points,
        "bot": opponent(db, attempt.day),
        "opening": opening,
    }


def start(db: Session, profile, *, ip: str = "") -> dict:
    """Start (or resume) today's attempt — WITHOUT starting the timer.

    A new attempt is returned with `served=False, remaining_sec=None`: the stamp
    is set by `begin()` once the client actually shows the question.

    Resuming first expires overdue questions (`pick=None`,
    `correct=False`, `pts=0`, `timeout=True`) — a closed tab must not
    freeze the timer. If the auto-timeout finished the last question, the attempt
    is closed right here and `{"done": True, "summary": …}` goes out
    (the next `start()` that day will raise `AlreadyPlayed`).
    """
    d = today()
    if not is_working(d):
        # Without this, Saturday created a separate attempt with Friday's bank.
        raise QuizClosed(_("Today is a day off — the quiz runs on working days"))
    bank_day = day_of(db, d)
    user_id = profile.user_id

    attempt = _attempt_for(db, user_id, d)
    if attempt is None:
        questions = _questions_for_day(db, bank_day)
        attempt = QuizAttempt(
            user_id=user_id, day=d, bank_day=bank_day,
            token=secrets.token_hex(16), started_at=_utcnow(),
            correct=0, points=0,
        )
        attempt.set_q_ids([q.id for q in questions])
        # No `served_at` at all: the first question's timer is started by `begin()`.
        attempt.set_answers([{"qid": questions[0].id}])
        try:
            with db.begin_nested():
                db.add(attempt)
                db.flush()
        except IntegrityError:
            # UNIQUE(user_id, day): a sibling worker got there first — read its row.
            db.expunge(attempt)
            attempt = _attempt_for(db, user_id, d)
            if attempt is None:
                raise QuizNotFound(_("attempt not found after the race"))

    if attempt.finished_at is not None:
        raise AlreadyPlayed(_("today's quiz has already been played"))

    answers = attempt.get_answers()
    q_ids = attempt.get_q_ids()
    index = sum(1 for a in answers if "pick" in a)
    if index >= len(q_ids):
        raise AlreadyPlayed(_("all questions have been answered"))

    # ── auto-timeout of overdue questions ──────────────────────────────
    question = None
    while index < len(q_ids):
        if len(answers) <= index:
            answers.append({"qid": q_ids[index]})
        entry = answers[index]
        question = _q_by_id(db, q_ids[index])
        if question is None:
            raise QuizUnavailable(_("the attempt's question has disappeared from the bank"))
        served = _parse_iso(entry.get("served_at"))
        if not _expired(question, served):
            break
        elapsed = (_utcnow() - served).total_seconds()
        entry.update({
            "qid": question.id, "pick": None, "ms": int(max(0.0, elapsed) * 1000),
            "correct": False, "pts": 0, "timeout": True,
        })
        index += 1
        question = None
    attempt.set_answers(answers)

    if index >= len(q_ids):
        summary = _finish(db, profile, attempt, ip=ip)
        return {"done": True, "summary": summary}

    db.flush()
    served = _parse_iso(answers[index].get("served_at"))
    sc = _score(attempt)
    bot = opponent(db, d)
    return {
        "token": attempt.token,
        "day": attempt.bank_day,
        "index": index,
        "total": len(q_ids),
        "question": public_question(question, index),
        "opponent": bot,
        "score": {"me": sc["correct"], "bot": bot},
        "served": served is not None,
        "remaining_sec": _remaining(question, served),
    }


def begin(db: Session, profile, token: str) -> dict:
    """Start the timer of the current question of attempt `token`.

    Idempotent: if `served_at` is already set, a second call writes nothing
    and returns the real remainder — a page reload (or a second
    tab) must not gift the time again.
    """
    user_id = profile.user_id
    attempt = db.query(QuizAttempt).filter(
        QuizAttempt.token == str(token or ""), QuizAttempt.user_id == user_id,
    ).first()
    if attempt is None:
        raise QuizNotFound(_("attempt not found"))
    if attempt.finished_at is not None:
        raise AlreadyPlayed(_("the attempt is already finished"))

    answers = attempt.get_answers()
    q_ids = attempt.get_q_ids()
    index = sum(1 for a in answers if "pick" in a)
    if index >= len(q_ids):
        raise AlreadyPlayed(_("all questions have been answered"))

    question = _q_by_id(db, q_ids[index])
    if question is None:
        raise QuizUnavailable(_("the attempt's question has disappeared from the bank"))

    if len(answers) <= index:
        answers.append({"qid": q_ids[index]})
    entry = answers[index]
    served = _parse_iso(entry.get("served_at"))
    if served is None:
        served = _utcnow()
        entry["qid"] = question.id
        entry["served_at"] = _iso(served)
        attempt.set_answers(answers)
        db.flush()

    sc = _score(attempt)
    bot = opponent(db, attempt.day)
    return {
        "index": index,
        "question": public_question(question, index),
        "served_at": _iso(served),
        "remaining_sec": _remaining(question, served),
        "score": {"me": sc["correct"], "bot": bot},
        "opponent": bot,
    }


def answer(db: Session, profile, token: str, pick: int | None, index: int | None = None,
           *, ip: str = "") -> dict:
    """Accept the answer to the current question of attempt `token`.

    `index` is optional, but when passed it guards against a double
    submit from two tabs: a mismatching question number means the
    answer was already counted, and it is not credited a second time (`QuizStale`).

    Everything — recording the answer and closing the attempt — happens in ONE
    SAVEPOINT over the row re-read with `with_for_update()`: otherwise two
    tabs that started the check at the same time would both see
    "not finished" and credit points twice. SQLite ignores `FOR UPDATE`,
    and that is fine: there is only one writing transaction there anyway.
    """
    user_id = profile.user_id
    attempt = db.query(QuizAttempt).filter(
        QuizAttempt.token == str(token or ""), QuizAttempt.user_id == user_id,
    ).first()
    if attempt is None:
        raise QuizNotFound(_("attempt not found"))
    if attempt.finished_at is not None:
        raise AlreadyPlayed(_("the attempt is already finished"))

    attempt_id = attempt.id
    with db.begin_nested():
        attempt = (
            db.query(QuizAttempt)
            .filter(QuizAttempt.id == attempt_id)
            .populate_existing().with_for_update().first()
        )
        if attempt is None:
            raise QuizNotFound(_("attempt not found"))
        if attempt.finished_at is not None:
            raise QuizStale(_("the attempt was already finished by another tab"))
        answers = attempt.get_answers()
        q_ids = attempt.get_q_ids()
        cur = sum(1 for a in answers if "pick" in a)
        if index is not None and int(index) != cur:
            raise QuizStale(_("question {index} has already been answered", index=index))
        if cur >= len(q_ids):
            raise AlreadyPlayed(_("all questions have been answered"))

        question = _q_by_id(db, q_ids[cur])
        if question is None:
            raise QuizUnavailable(_("the attempt's question has disappeared from the bank"))

        entry = answers[cur] if len(answers) > cur else {"qid": q_ids[cur]}
        served = _parse_iso(entry.get("served_at"))
        if served is None:  # the client answered without calling `begin()` — time from zero
            served = _utcnow()
            entry["served_at"] = _iso(served)
        elapsed = (_utcnow() - served).total_seconds()
        if elapsed < 0:
            elapsed = 0.0

        timeout = elapsed > (question.sec + GRACE_SEC)
        correct = (pick is not None and int(pick) == question.answer_idx and not timeout)
        pts = (10 + (5 if elapsed <= question.sec / 2 else 0)) if correct else 0

        entry.update({
            "qid": question.id,
            "served_at": entry.get("served_at") or _iso(served),
            "pick": None if pick is None else int(pick),
            "ms": int(elapsed * 1000),
            "correct": bool(correct),
            "pts": int(pts),
        })
        if timeout:
            entry["timeout"] = True
        if len(answers) > cur:
            answers[cur] = entry
        else:
            answers.append(entry)

        done = (cur + 1) >= len(q_ids)
        nxt = None
        if not done:
            nq = _q_by_id(db, q_ids[cur + 1])
            if nq is None:
                raise QuizUnavailable(_("the next question has disappeared from the bank"))
            # The next question goes to the client together with the verdict, but its
            # timer does NOT start HERE: while the person reads the explanation of
            # this answer, the next question is not shown yet. Until 2026-09-10
            # the stamp was set at this moment, and reading the explanation was charged
            # to the next question — a correct answer after 62 s with a 60 s limit
            # went to "time's up". The clock is started by `begin()` when the client
            # actually shows the question (it is idempotent and picks
            # the current index itself).
            answers.append({"qid": nq.id})
            nxt = public_question(nq, cur + 1)
            nxt["served"] = False
            nxt["remaining_sec"] = int(nq.sec)
        attempt.set_answers(answers)
        db.flush()

        summary = _finish(db, profile, attempt, ip=ip) if done else None

    sc = _score(attempt)
    bot = opponent(db, attempt.day)
    out = {
        "correct": bool(correct),
        "answer_idx": question.answer_idx,
        # The explanation is the same question, only after the answer: same language.
        "explanation": question.localized(i18n.current_lang())["e"],
        "points": int(pts),
        "elapsed_ms": int(elapsed * 1000),
        "score": {"me": sc["correct"], "bot": bot},
        "next": nxt,
        "done": bool(done),
    }
    if summary is not None:
        out["summary"] = summary
    return out


def status(db: Session, profile) -> dict:
    """Quiz state for the quests page."""
    d = today()
    bank_day = day_of(db, d)
    attempt = _attempt_for(db, profile.user_id, d)
    played = bool(attempt is not None and attempt.finished_at is not None)
    last = None
    if attempt is not None and attempt.finished_at is not None:
        last = {"correct": attempt.correct, "points": attempt.points}
    have = db.query(func.count(QuizQuestion.id)).filter(
        QuizQuestion.active.is_(True), QuizQuestion.day == bank_day).scalar() or 0
    return {
        "played_today": played,
        "last": last,
        "bank_day": bank_day,
        "working_today": is_working(d),
        "available": bool(have) and is_working(d),
        "quiz_correct_total": int(profile.quiz_correct_total or 0),
        "level": level(int(profile.quiz_correct_total or 0)),
    }
