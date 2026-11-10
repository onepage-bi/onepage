"""The "Working on the platform" course: routing over `app/services/learn.py`.

A thin layer following the `app/api/quests.py` canon: the service computes and writes rows,
the route owns the transaction, auditing happens here too. Nothing on the hot path: the course
page and the two endpoints run only on explicit user actions.

Correct answers don't leave the server before the answer: `GET /api/learn/state` returns
the question and options, `POST /api/learn/check` — the feedback. A lesson's task
is confirmed by platform state (`learn.verify_task`), not by the client's
word.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import i18n
from app.auth.deps import get_current_user
from app.config import settings
from app.database import get_db
from app.models import AuditLog, Employee, User
from app.services import learn, quests, quests_config

router = APIRouter()

_MAX_ANSWERS = 16          # the longest lesson has two checks; headroom for the future


def _log(db: Session, actor: User, action: str, resource_id: str, detail: dict) -> None:
    """`app/api/favorites.py` canon: we add the row, the caller commits."""
    db.add(AuditLog(actor_id=actor.id, action=action,
                    resource_type="learn", resource_id=resource_id,
                    detail=json.dumps(detail, ensure_ascii=False)))


async def _state(db: Session, user: User, course: str, lang: str) -> dict:
    """Course state plus its reward.

    The reward lives in Quests (its mechanics and its art are there), but it must be shown by
    the Practicum — at the moment the person reaches the end. Hence the glue lives here,
    in routing: the services still don't know about each other.
    """
    state = await run_in_threadpool(learn.state, db, user, course, lang)
    # No Quests, no reward: the card, its capsule and the "Open the card" link
    # all hang off `state.reward`, so while Quests is off (the default) the
    # finale shows only the diploma and nothing points at a 404.
    quests_config.ensure_fresh()
    state["reward"] = quests.special_for_course(course) if settings.quests_enabled else None
    return state


def _course_logged(db: Session, user: User, course: str) -> bool:
    """Has completion of this course by this person already been recorded?

    Not a guard against a race (there is none here: the row is written in the same transaction as
    the last lesson), but against a repeated "completion" after a chapter was added
    to the course: the course becomes incomplete again, and a second record
    would look like a second diploma.
    """
    return db.query(AuditLog.id).filter(
        AuditLog.actor_id == user.id,
        AuditLog.action == "learn_course_done",
        AuditLog.resource_id == course,
    ).first() is not None


class CheckIn(BaseModel):
    # chapter or exam id ("u3:exam")
    lesson: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9:_-]+$")
    answers: list[int] = Field(default_factory=list, max_length=_MAX_ANSWERS)


@router.get("/learn", response_class=HTMLResponse)
async def learn_index(request: Request,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """Course showcase: prep and base, with progress for each."""
    env: Environment = request.app.state.jinja
    lang = i18n.current_lang()
    cards = []
    for course in learn.COURSES:
        st = await _state(db, user, course, lang)
        cards.append({
            "id": course,
            "title": st["title"], "subtitle": st["subtitle"],
            "done": st["done"], "total": st["lessons_total"],
            "minutes": st["minutes_total"], "xp": st["xp"], "xp_total": st["xp_total"],
            "level": st["level"]["name"],
            "art": "prep" if course == "prep" else "base",
        })
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/learn_index.html").render,
        user=user, request=request, cards=cards))


@router.get("/learn/{course}", response_class=HTMLResponse)
async def learn_page(course: str, request: Request,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    if course not in learn.COURSES:
        raise HTTPException(status_code=404, detail=i18n._("No such course"))
    env: Environment = request.app.state.jinja
    state = await _state(db, user, course, i18n.current_lang())
    # State travels as an attribute, not a separate <script> — like `#q-root` in
    # quests: the payload goes through attribute escaping rather than
    # through script concatenation.
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/learn.html").render,
        user=user, request=request, course=course, course_title=state["title"],
        state_json=json.dumps(state, ensure_ascii=False)))


def _person(db: Session, user: User) -> dict:
    """Who gets the diploma: full name from the account, title and department from HR.

    The directory is not a prerequisite: some people have no row in it
    (`app/services/identity.py`, 6 UPN accounts after the merge), and the diploma
    is printed with just the name rather than refusing to print at all.
    """
    emp = (db.query(Employee)
           .filter(Employee.person_id == user.person_id, Employee.is_active.is_(True))
           .order_by(Employee.id.desc()).first())
    name = (user.display_name or "").strip()
    if not name and emp:
        name = " ".join(x for x in (emp.lastname_ru, emp.firstname_ru,
                                    emp.middleinitial_ru) if x).strip()
    return {
        "name": name or user.person_id,
        "title": (emp.jobtitle_ru or "").strip() if emp else "",
        "dept": (emp.deptname_ru or "").strip() if emp else "",
    }


def _diploma_no(user_id: int, course: str, finished_at) -> str:
    """Diploma number: reproducible, so it doesn't need to be stored."""
    raw = "%d|%s|%s" % (user_id, course, finished_at.isoformat() if finished_at else "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8].upper()


@router.get("/learn/{course}/diploma", response_class=HTMLResponse)
async def learn_diploma(course: str, request: Request,
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Diploma for a completed course — only for its owner and only once actually earned.

    No diploma in advance: the server does the check (`learn.course_done`),
    not a hidden button. Not completed — we send the person back to the course rather than
    showing an empty form.
    """
    if course not in learn.COURSES:
        raise HTTPException(status_code=404, detail=i18n._("No such course"))
    lang = i18n.current_lang()
    fin = await run_in_threadpool(learn.course_done, db, user, course, lang)
    if not fin:
        return RedirectResponse("/learn/%s" % course, status_code=302)
    person = await run_in_threadpool(_person, db, user)
    env: Environment = request.app.state.jinja
    return HTMLResponse(await run_in_threadpool(
        env.get_template("pages/diploma.html").render,
        user=user, request=request, course=course, fin=fin, person=person,
        number=_diploma_no(user.id, course, fin["finished_at"]),
        finished=fin["finished_at"]))


@router.get("/api/learn/exam/{unit_id}")
async def learn_exam(unit_id: str,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Exam questions without answers — the selection is fixed per person."""
    lang = i18n.current_lang()
    lesson = await run_in_threadpool(learn.exam_lesson, unit_id, user.id, lang)
    if lesson is None:
        raise HTTPException(status_code=404, detail=i18n._("No such unit"))
    if not learn.is_unlocked(db, user, learn.exam_id(unit_id), lang):
        raise HTTPException(status_code=409, detail=i18n._("Finish every chapter of the unit first"))
    done = learn.exam_id(unit_id) in learn._done_ids(db, user)
    return learn.public_lesson(lesson, done, True)


@router.get("/api/learn/state")
async def learn_state(course: str = learn.DEFAULT_COURSE,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    if course not in learn.COURSES:
        raise HTTPException(status_code=404, detail=i18n._("No such course"))
    return await _state(db, user, course, i18n.current_lang())


@router.post("/api/learn/check")
async def learn_check(body: CheckIn,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    lang = i18n.current_lang()
    if learn.lesson_by_id(body.lesson, lang, user_id=user.id) is None:
        raise HTTPException(status_code=404, detail=i18n._("No such lesson"))
    if not learn.is_unlocked(db, user, body.lesson, lang):
        # Lessons go in order — otherwise the "course" turns into a list of articles.
        raise HTTPException(status_code=409, detail=i18n._("This lesson is still locked"))
    result = await run_in_threadpool(learn.check, db, user, body.lesson, body.answers, lang)
    course = learn.course_of(body.lesson, lang) or learn.DEFAULT_COURSE
    if result["completed"]:
        _log(db, user, "learn_lesson_done", body.lesson,
             {"xp": (learn.lesson_by_id(body.lesson, lang, user_id=user.id) or {}).get("xp"),
              "mode": result.get("mode")})
        # The moment the course became completed happens exactly once — and it must
        # leave a trace, not be recomputed by everyone who needs it.
        # The audit query runs only here, on the rare "lesson closed" path,
        # and only if the course actually got closed.
        fin = await run_in_threadpool(learn.course_done, db, user, course, lang)
        if fin and not _course_logged(db, user, course):
            _log(db, user, "learn_course_done", course,
                 {"lessons": fin["lessons"], "xp": fin["xp"], "level": fin["level"]})
            result["course_done"] = True
            # The award ceremony lives in Quests — where the person opens
            # any other surprise. The Practicum only puts a card in the queue;
            # if Quests are disabled, the course doesn't break.
            quests_config.ensure_fresh()
            if settings.quests_enabled:
                quests.grant_special_opening(db, user.id, course)
    db.commit()
    result["state"] = await _state(db, user, course, lang)
    return result


@router.post("/api/learn/task/{lesson_id}")
async def learn_task(lesson_id: str,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """A standalone task check — so that "Check" works before the answers."""
    lang = i18n.current_lang()
    lesson = learn.lesson_by_id(lesson_id, lang)
    if lesson is None:
        raise HTTPException(status_code=404, detail=i18n._("No such lesson"))
    task = lesson.get("task") or {}
    done = await run_in_threadpool(learn.verify_task, db, user, task.get("kind"))
    return {"done": done, "hint": "" if done else task.get("hint", "")}
