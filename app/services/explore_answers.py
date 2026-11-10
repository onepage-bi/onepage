"""Server-side recording of an AI answer without data (see docs/LLM_PROVIDERS.md, section
"Recording the answer").

One row per successful `/explore` answer, written into `explore_answers`.
Deliberately thin and deliberately NOT the result: what is recorded is enough
to audit «what was asked, against which frozen semantics, what shape of SQL
answered it» without ever holding a result cell or the SQL text itself (a
filter literal can quote real data — the same reason `/api/explore/*` never
logs full SQL, only `sql[:500]` in the audit log's own `detail`, and even
that is a truncated PREVIEW, not raw storage discipline for this table).

`record()` only `db.add()`s — same discipline as `_log()`/`AuditLog` calls
elsewhere in `app/api/*`: the caller's own `db.commit()` is what makes it
durable, so a record here is atomic with whatever else that request wrote."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ExploreAnswer, ExploreFormSuggestion, GeneratorVersion, User


def _sha256(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_version_info(db: Session, generator_id: int) -> tuple[int, str] | None:
    """(version_number, semantics_sha256) of the generator's CURRENT version,
    or None when there is no current version or it carries no semantics —
    callers treat that as «nothing to record», not an error: recording an
    answer must never be the reason a question fails."""
    ver = (db.query(GeneratorVersion)
           .filter(GeneratorVersion.generator_id == generator_id,
                   GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    if ver is None or not ver.semantics_json:
        return None
    return ver.version_number, _sha256(ver.semantics_json)


def record(
    db: Session, user_id: int, generator_id: int, question: str, tier: str, via: str, *,
    dashboard_id: int | None = None,
    intent: dict | None = None,
    sql: str | None = None,
    run_id: int | None = None,
    version_info: tuple[int, str] | None = None,
) -> ExploreAnswer | None:
    """Add one `ExploreAnswer` row (not committed — see module docstring) and
    return it, or None when nothing was recorded.

    The row is FLUSHED (not committed) so the caller can hand `answer.id` back
    to the page: 👍/👎 needs a server id, and every route that answers a
    question writes the row and returns within the same request. The whole
    body is guarded: "recording an answer never breaks the request" is the rule this
    module was built around, so any failure here — a flush that hits a
    constraint, a broken semantics blob — returns None and leaves the answer
    itself alone (the page simply gets no `answer_id` and keeps its vote
    local).

    Silently a no-op when the generator has no CURRENT frozen semantics to
    anchor the record to (an answer must have been grounded against SOME
    semantics to exist at all, but a race with a republish is not worth
    failing the request over) — pass `version_info` when the caller already
    resolved it (avoids a second query the caller just did)."""
    try:
        info = version_info if version_info is not None else current_version_info(db, generator_id)
        if info is None:
            return None
        version_number, semantics_sha256 = info
        row = ExploreAnswer(
            user_id=user_id,
            generator_id=generator_id,
            dashboard_id=dashboard_id,
            question=question,
            tier=tier,
            intent_json=json.dumps(intent, ensure_ascii=False) if intent is not None else None,
            sql_sha256=_sha256(sql),
            generator_version=version_number,
            semantics_sha256=semantics_sha256,
            run_id=run_id,
            via=via,
        )
        # SAVEPOINT, not a bare add()+flush(): a flush that hits a constraint
        # leaves the whole session in pending-rollback, and the caller's own
        # `db.commit()` — the audit row, the answer's own bookkeeping — then
        # dies with PendingRollbackError, turning "an answer without answer_id" into
        # a 500. The nested transaction rolls back ONLY this INSERT (and
        # takes the pending row back out of the session with it), so the
        # request that produced the answer commits exactly as if `record()`
        # had never been called.
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except Exception:
        # The answer is already on the user's screen; a bookkeeping row is
        # never a reason to fail the request that produced it.
        return None


# ─────────────────────────── 👍/👎 and the suggestion bank ─────────────────
# A vote is the only thing that changes in a recorded answer, and the only entry
# into `explore_form_suggestions`: there is no separate "suggest a form". The reason
# is exactly one path for creating a suggestion: the client cannot invent a suggestion
# for someone else's (or a non-existent) answer, because a suggestion is never
# created by anything except a vote on YOUR OWN answer.
#
# Rules (P4):
#   * 👎 → a kind-B suggestion: "this question was answered wrongly" — the report
#     author should look at the metric's definition;
#   * an INDEPENDENTLY confirmed 👍 on one normalised question for one
#     generator → a kind-A suggestion: "this question gets asked, and the answer is right" —
#     a candidate for `question_forms`. "Independently" means either three 👍 from one
#     person on THREE DIFFERENT CALENDAR DAYS, or 👍 from TWO DIFFERENT people
#     (see `_upvote_signal`). A further 👍 does not create a new suggestion.

VOTES = ("up", "down")

# On how many DIFFERENT calendar days one person's 👍 must repeat
# for the question to become a form candidate.
UPVOTE_DAYS_FOR_FORM = 3

# How many DIFFERENT people saying "correct" on one question give the same signal
# at once.
UPVOTERS_FOR_FORM = 2

# How many voted answers for this generator to look back over when
# counting. A vote is a rare event, and a question asked several times sits
# close together in time; the cap only keeps the count from growing with history.
_VOTE_SCAN_LIMIT = 500

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+", re.UNICODE)


def normalize_question(question: str) -> str:
    """"How many  SL per week?!" → "how many sl per week".

    Case, extra spaces and punctuation do not make different questions. NFKC before
    casefold, so that different Unicode spellings of one letter do not split one question
    into two suggestions."""
    text = unicodedata.normalize("NFKC", str(question or ""))
    return _SPACES.sub(" ", _PUNCT.sub(" ", text.casefold())).strip()


def question_hash(question: str) -> str:
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()


def vote(db: Session, user: User, answer_id: int, value: str | None) -> dict | None:
    """Set/clear 👍/👎 on YOUR OWN answer.

    Returns `{"vote", "suggested", "suggestion"}` (the last one is the suggestion
    row itself if the vote created it: the route writes the audit, it has the request)
    or None if the answer does not exist OR belongs to someone else:
    the calling route answers both cases with 404 — a 403 on someone else's id would reveal
    that such an answer exists.

    Idempotent: the same vote a second time is a no-op (and no second
    suggestion), a different one is an update, `None` clears it. Does not commit: the transaction
    belongs to the route, as everywhere in `app/api/*`."""
    if value is not None and value not in VOTES:
        return None
    ans = db.query(ExploreAnswer).filter(ExploreAnswer.id == answer_id).first()
    if ans is None or ans.user_id != user.id:
        return None
    if ans.vote == value:
        return {"vote": ans.vote, "suggested": False, "suggestion": None}
    ans.vote = value
    ans.voted_at = datetime.utcnow() if value is not None else None
    db.flush()
    suggestion = None
    if value == "down":
        suggestion = suggest_form(db, user, ans, "B")
    elif value == "up" and _upvote_signal(db, ans):
        suggestion = suggest_form(db, user, ans, "A")
    return {"vote": ans.vote, "suggested": suggestion is not None,
            "suggestion": suggestion}


def _upvote_signal(db: Session, answer: ExploreAnswer) -> bool:
    """Are there enough 👍 on THIS SAME question (by normalised text) in this
    generator to suggest it to the report author as a form.

    Simply counting "three 👍" will not do: regenerating an answer is one button, and one
    person inflated a `question_forms` candidate with three clicks in a row within
    a minute. The signal must be INDEPENDENT, and independence comes in two kinds:

      * in TIME — the same person came back to the same question on three different
        calendar days (`UPVOTE_DAYS_FOR_FORM`): "I use this question
        regularly", not "I clicked three times";
      * in PEOPLE — 👍 came from `UPVOTERS_FOR_FORM` different people: one person
        has no way to inflate someone else's account, and two independent "the answer is right" are already
        stronger than three of one's own.

    EITHER of the two is enough: these are not two different rules but one — "the question
    is confirmed by more than one click". The day is a calendar day (the UTC date of
    `voted_at`, as `vote()` writes it), not "24 hours": a day boundary
    can be explained to a person, a sliding window cannot.

    The question is normalised in Python, not in SQL: SQLite cannot do
    Unicode-aware `casefold`, and storing a second, normalised, question text
    next to the first would mean keeping the question in the database twice."""
    target = question_hash(answer.question)
    rows = (db.query(ExploreAnswer.user_id, ExploreAnswer.question, ExploreAnswer.voted_at)
            .filter(ExploreAnswer.generator_id == answer.generator_id,
                    ExploreAnswer.vote == "up")
            .order_by(ExploreAnswer.id.desc())
            .limit(_VOTE_SCAN_LIMIT)
            .all())
    voters: set[int] = set()
    days: dict[int, set] = {}
    for uid, question, voted_at in rows:
        if question_hash(question) != target:
            continue
        voters.add(uid)
        if voted_at is not None:
            days.setdefault(uid, set()).add(voted_at.date())
    if len(voters) >= UPVOTERS_FOR_FORM:
        return True
    return any(len(d) >= UPVOTE_DAYS_FOR_FORM for d in days.values())


def suggest_form(db: Session, user: User, answer: ExploreAnswer,
                 kind: str) -> ExploreFormSuggestion | None:
    """Put one suggestion for answer `answer` into the author's inbox, or None
    if an identical one is already waiting unreviewed.

    Deduplication is a PARTIAL unique index on (generator, question hash,
    kind) `WHERE status='new'`: a full UNIQUE would stop a question from returning to
    the queue after `declined`, and "declined half a year ago, asked again" is
    exactly a new signal, not a duplicate. The SELECT check stays as
    the fast path, but the index decides: two simultaneous 👎 on one question both
    used to pass the check and insert a row each, and the author's queue doubled.

    By construction there are no result rows or SQL text here: the row carries
    the question, the metric key, the intent and the SQL HASH."""
    if kind not in ("A", "B"):
        return None
    qhash = question_hash(answer.question)
    exists = (db.query(ExploreFormSuggestion.id)
              .filter(ExploreFormSuggestion.generator_id == answer.generator_id,
                      ExploreFormSuggestion.question_hash == qhash,
                      ExploreFormSuggestion.kind == kind,
                      ExploreFormSuggestion.status == "new")
              .first())
    if exists is not None:
        return None
    intent = answer.get_intent() or {}
    row = ExploreFormSuggestion(
        generator_id=answer.generator_id,
        user_id=user.id,
        answer_id=answer.id,
        question=answer.question,
        question_hash=qhash,
        kpi_key=(str(intent.get("kpi"))[:64] if intent.get("kpi") else None),
        intent_json=answer.intent_json,
        sql_hash=answer.sql_sha256,
        kind=kind,
        status="new",
    )
    # SAVEPOINT: violating the partial UNIQUE means "already there", not a query
    # error, and exactly this insert must be rolled back, not the route's whole transaction
    # (same reason as in `record()`).
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    return row
