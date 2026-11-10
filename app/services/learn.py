"""The "Working on the platform" course: short lessons with checks and tasks.

Why a separate tab when the Dev Guide exists: the reference answers the question
"how is this done", while the course answers "where do I start and what's next".
Someone opening the platform for the first time doesn't know which questions they
haven't asked yet — so lessons go in order, each one unlocks the next, and
each ends with a check rather than a promise that everything is clear.

Three things keep the course honest:

* **Answers live only on the server.** The client gets the question and options, but
  never `answer`/`why` before answering — otherwise the "check" turns into
  decoration (the same discipline as the quest quiz bank).
* **A task is verified against platform state, not an "I did it" button.**
  "Upload a report" is confirmed by a row in `dashboards`, "make a link" —
  in `share_tokens`. The only way to cheat is to actually do it.
* **XP is awarded once.** A `learn_progress` row is unique per
  (user, lesson); a retake earns no points a second time, and if two tabs
  race, the database rejects the second insert.

Content is not code: `app/content/learn.<lang>.json`, one file per language, with
a fallback to English. The correct answers live there too, so the file never
goes to the client whole — the page gets what `state()` returned.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.i18n import _
from app.models import (
    Dashboard, DashboardFavorite, DashboardPermission, GenerationRun, LearnProgress,
    ShareToken, User, has_permission,
)

CONTENT_DIR = Path(__file__).resolve().parents[1] / "content"
DEFAULT_LANG = "en"

# There is more than one course: the prep course gets you to your first report, the base
# course explains the platform. One file per (course, language): app/content/learn.<course>.<lang>.json.
# Chapter and block ids MUST be unique across courses — both progress
# (`learn_progress.lesson_id`) and the "which course does this chapter
# belong to" lookup rely on them (pinned by a test).
COURSES = ("prep", "base")
DEFAULT_COURSE = "prep"

# Tasks: what exactly the platform counts as done. Each is one EXISTS over
# an already existing table, no new telemetry (and no trust in the
# client). The key comes from the content, the value is a function (db, user) -> bool.
TASKS = {
    "upload_report": lambda db, user: db.scalar(
        select(func.count()).select_from(Dashboard).where(Dashboard.owner_id == user.id)) > 0,
    "favorite": lambda db, user: db.scalar(
        select(func.count()).select_from(DashboardFavorite)
        .where(DashboardFavorite.user_id == user.id)) > 0,
    "share_link": lambda db, user: db.scalar(
        select(func.count()).select_from(ShareToken)
        .where(ShareToken.created_by == user.id)) > 0,
    "run_generator": lambda db, user: db.scalar(
        select(func.count()).select_from(GenerationRun)
        .where(GenerationRun.started_by == user.id)) > 0,
    # A second report version is the only course task that can't be
    # done by accident: the first upload gives version 1, and only a deliberate
    # edit ("Upload new version") bumps the counter. The course requires it because
    # the first version of a report is always wrong, and a person should go through the loop
    # "found a bug — fixed it — published", not stop at the first upload.
    "new_version": lambda db, user: db.scalar(
        select(func.count()).select_from(Dashboard)
        .where(Dashboard.owner_id == user.id, Dashboard.current_version >= 2)) > 0,
}

# A task must not turn into a wall. Not everyone has the "upload a report"
# right, and someone without it shouldn't be stuck on chapter three forever:
# a task blocks the chapter ONLY if this person can perform it
# at all. Otherwise it is shown as optional practice.
TASK_POSSIBLE = {
    "upload_report": lambda db, user: has_permission(user.role, "upload_dashboard"),
    "run_generator": lambda db, user: has_permission(user.role, "upload_dashboard"),
    "new_version": lambda db, user: has_permission(user.role, "upload_dashboard"),
    "favorite": lambda db, user: db.scalar(
        select(func.count()).select_from(Dashboard).where(
            (Dashboard.is_public.is_(True)) | (Dashboard.owner_id == user.id) |
            (Dashboard.id.in_(select(DashboardPermission.dashboard_id)
                              .where(DashboardPermission.user_id == user.id))))) > 0,
    "share_link": lambda db, user: db.scalar(
        select(func.count()).select_from(Dashboard).where(
            (Dashboard.owner_id == user.id) |
            (Dashboard.id.in_(select(DashboardPermission.dashboard_id).where(
                (DashboardPermission.user_id == user.id) &
                (DashboardPermission.can_update_data.is_(True))))))) > 0,
}

_cache: dict[str, tuple[float, dict]] = {}


def task_possible(db: Session, user: User, kind: Optional[str]) -> bool:
    fn = TASK_POSSIBLE.get(kind or "")
    if fn is None:
        return True
    try:
        return bool(fn(db, user))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def content(course: str = DEFAULT_COURSE, lang: str = DEFAULT_LANG) -> dict:
    """The course in this language, falling back to English. Cached until mtime changes."""
    if course not in COURSES:
        course = DEFAULT_COURSE
    path = CONTENT_DIR / f"learn.{course}.{lang}.json"
    if not path.exists():
        path = CONTENT_DIR / f"learn.{course}.{DEFAULT_LANG}.json"
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {"units": [], "levels": []}
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"units": [], "levels": []}
    _cache[str(path)] = (mtime, data)
    return data


def content_lang(course: str = DEFAULT_COURSE, lang: str = DEFAULT_LANG) -> str:
    """Which language the person will actually see the chapters in.

    The UI is translated, the content may not be: a file exists only for the languages
    the course is written in. Staying silent about it is dishonest — the page must say
    that the reading will be in the fallback language, not pretend it was meant that way."""
    return lang if (CONTENT_DIR / f"learn.{course}.{lang}.json").exists() else DEFAULT_LANG


def lessons(course: str = DEFAULT_COURSE, lang: str = DEFAULT_LANG) -> list[dict]:
    """All lessons of the course in order — the order is the unlock rule."""
    out: list[dict] = []
    for unit in content(course, lang).get("units", []):
        for lesson in unit.get("lessons", []):
            out.append(lesson)
    return out


def course_of(lesson_id: str, lang: str = DEFAULT_LANG) -> Optional[str]:
    """Which course this chapter, block or exam belongs to."""
    unit_id = lesson_id[: -len(EXAM_SUFFIX)] if is_exam(lesson_id) else None
    for course in COURSES:
        for unit in content(course, lang).get("units", []):
            if unit_id and unit.get("id") == unit_id:
                return course
            for lesson in unit.get("lessons", []):
                if lesson.get("id") == lesson_id:
                    return course
    return None


def lesson_by_id(lesson_id: str, lang: str = DEFAULT_LANG,
                 user_id: Optional[int] = None) -> Optional[dict]:
    course = course_of(lesson_id, lang)
    if course is None:
        return None
    if is_exam(lesson_id):
        if user_id is None:
            return None
        return exam_lesson(lesson_id[: -len(EXAM_SUFFIX)], user_id, lang)
    for lesson in lessons(course, lang):
        if lesson.get("id") == lesson_id:
            return lesson
    return None


def has_exams(course: str = DEFAULT_COURSE, lang: str = DEFAULT_LANG) -> bool:
    """A course may have no exams: the prep course leads to the first
    report, and an exam there would be a barrier rather than a check."""
    return content(course, lang).get("exams", True) is not False


def total_xp(course: str = DEFAULT_COURSE, lang: str = DEFAULT_LANG) -> int:
    total = sum(int(l.get("xp") or 0) for l in lessons(course, lang))
    if has_exams(course, lang):
        total += EXAM_XP * len(content(course, lang).get("units", []))
    return total


# ---------------------------------------------------------------------------
# Block exam
# ---------------------------------------------------------------------------
#
# A course ends not with the last chapter but with the question "did you really get it".
# The exam is assembled from the block's own questions, but behaves differently from
# a lesson: no per-question feedback along the way, answers are submitted all at once, the threshold is at least
# EXAM_PASS of the number of questions. Retakes are unlimited: the selection is
# deterministic per (person, block), so a second attempt is the same work
# on the same questions, not a lottery.

EXAM_SUFFIX = ":exam"
EXAM_SIZE = 6
EXAM_XP = 60
EXAM_PASS = 0.8


def exam_id(unit_id: str) -> str:
    return unit_id + EXAM_SUFFIX


def is_exam(lesson_id: str) -> bool:
    return lesson_id.endswith(EXAM_SUFFIX)


def unit_by_id(unit_id: str, lang: str = DEFAULT_LANG) -> Optional[dict]:
    for course in COURSES:
        for unit in content(course, lang).get("units", []):
            if unit.get("id") == unit_id:
                return unit
    return None


def exam_lesson(unit_id: str, user_id: int, lang: str = DEFAULT_LANG) -> Optional[dict]:
    """Pseudo-lesson: the same fields as a chapter, so both the server and the page
    handle an exam with the same code as a lesson."""
    unit = unit_by_id(unit_id, lang)
    if unit is None:
        return None
    # Select per chapter, not from one pile: six random questions out of 25
    # can easily come from just two chapters, and the exam stops testing the
    # block. Round-robin — first one question from each chapter.
    rng = random.Random(hashlib.sha1(("%s:%s" % (user_id, unit_id)).encode()).hexdigest())
    by_lesson = []
    for lesson in unit.get("lessons", []):
        # A question in the exam lives without its chapter, so it carries the chapter's
        # title: the prompt must stand on its own, but reminding which part
        # of the block it's about is fair and helps focus.
        qs = [dict(c, **{"from": lesson.get("title")}) for c in (lesson.get("checks") or [])]
        rng.shuffle(qs)
        if qs:
            by_lesson.append(qs)
    if not by_lesson:
        return None
    rng.shuffle(by_lesson)
    picked = []
    while len(picked) < EXAM_SIZE and any(by_lesson):
        for qs in by_lesson:
            if qs and len(picked) < EXAM_SIZE:
                picked.append(qs.pop())
        by_lesson = [qs for qs in by_lesson if qs]
    rng.shuffle(picked)
    return {
        "id": exam_id(unit_id),
        "title": _("Unit test: {unit}").format(unit=unit.get("title") or ""),
        "summary": _("Questions from the whole unit. The review comes after you submit."),
        "minutes": 10,
        "xp": EXAM_XP,
        "mode": "exam",
        "cards": [{
            "heading": _("Unit test {ctx:2}"),
            "text": _("Below are {n} questions from every chapter of the unit. There is no review along the way: you answer them all, then see the score and the explanations.\n\nThe pass mark is {need} of {n}. Retake as often as you like — the questions are the same: this checks understanding, not luck.").format(
                          n=len(picked), need=_pass_mark(len(picked))),
        }],
        "checks": picked,
        "takeaways": [],
        "sources": [],
    }


def _pass_mark(n: int) -> int:
    return max(1, int(n * EXAM_PASS + 0.999))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _done_ids(db: Session, user: User) -> set[str]:
    rows = db.execute(
        select(LearnProgress.lesson_id).where(LearnProgress.user_id == user.id)
    ).scalars().all()
    return set(rows)


def level_for(xp: int, course: str = DEFAULT_COURSE, lang: str = DEFAULT_LANG) -> dict:
    """Current level and the next threshold — for the progress bar in the header."""
    levels = content(course, lang).get("levels") or [{"xp": 0, "name": ""}]
    cur = levels[0]
    nxt = None
    for i, lv in enumerate(levels):
        if xp >= int(lv.get("xp") or 0):
            cur = lv
            nxt = levels[i + 1] if i + 1 < len(levels) else None
    return {
        "name": cur.get("name", ""),
        "at": int(cur.get("xp") or 0),
        "next_name": (nxt or {}).get("name"),
        "next_at": int(nxt["xp"]) if nxt else None,
    }


# What a card can carry. A course chapter is a handbook, not a hint:
# heading, paragraphs, list, table, code, screenshot, callout.
# `code_title` — the caption ABOVE a code block. By default it says "Copy this
# text in full", and for a prompt to the model that's true; but the course uses the same block
# to show a form people fill in for themselves, and calling it "copy" would
# lie about the main action. The field is optional; content overrides the
# caption where it doesn't fit.
CARD_FIELDS = ("heading", "text", "list", "table", "code_text", "code_title",
               "editable", "image", "note", "file",
               "cast")

# Card images live in two places: product screenshots (shared with the Dev Guide) and
# the course's drawn illustrations. Content names the file, and the server builds
# the URL — so the author doesn't need to remember where things live.
IMG_DIRS = (("learn", Path(__file__).resolve().parents[2] / "static" / "img" / "learn"),
            ("docs", Path(__file__).resolve().parents[2] / "static" / "img" / "docs"))


def image_url(name: str) -> str:
    for folder, path in IMG_DIRS:
        if (path / f"{name}.webp").exists():
            return f"/static/img/{folder}/{name}.webp"
    return f"/static/img/docs/{name}.webp"


# A lesson can give the person a file — a training export on which they build their
# first report (`tools/learn_fixture_delta.py`). Plain static serving hands it out:
# the card names the FILE NAME, the server builds the URL.
FILES_DIR = Path(__file__).resolve().parents[2] / "static" / "learn" / "files"
# The name comes from content, but content is edited by hand, and a public path must never
# be built from a foreign string under any circumstances: a character whitelist — the
# same rule as for images, just written out explicitly, because here the name
# carries the extension.
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def file_url(name: str) -> Optional[str]:
    """URL of the training file — or None if there is no such file.

    "No file" must mean "no field", not a link to nowhere: a "Download"
    button leading to a 404 is worse than a missing button."""
    if not name or not _FILE_NAME.match(name) or ".." in name:
        return None
    if not (FILES_DIR / name).is_file():
        return None
    return f"/static/learn/files/{name}"


def public_lesson(lesson: dict, done: bool, unlocked: bool,
                  snippets: Optional[dict] = None) -> dict:
    """The lesson as the browser sees it: without correct answers or explanations.

    Code snippets are substituted here: in the content a card refers to them by
    name, and the text itself lives next to the lessons — it is as much part of the course's
    language as everything else."""
    snippets = snippets or {}
    cards = []
    for card in lesson.get("cards") or []:
        out = {k: card[k] for k in CARD_FIELDS if card.get(k)}
        if out.get("image"):
            out["image_url"] = image_url(out["image"])
        if out.get("file"):
            # A card can serve one file or several: the sample report and
            # its export are needed at the same time, and spreading them across
            # adjacent screens means losing one of the two.
            names = out["file"] if isinstance(out["file"], list) else [out["file"]]
            files = [{"name": n, "url": u}
                     for n in names for u in [file_url(n)] if u]
            if files:
                out["files"] = files
                out["file"] = files[0]["name"]
                out["file_url"] = files[0]["url"]
            else:
                out.pop("file")          # no file — no button
        if card.get("code"):
            out["code_text"] = snippets.get(card["code"], "")
        cards.append(out)
    return {
        "id": lesson.get("id"),
        "title": lesson.get("title"),
        "summary": lesson.get("summary"),
        "minutes": lesson.get("minutes"),
        "xp": lesson.get("xp"),
        "cards": cards,
        "takeaways": lesson.get("takeaways") or [],
        "sources": lesson.get("sources") or [],
        # `kind` — question type (analysis, counterfactual, estimate, spot the error,
        # design decision). It is visible to the person and sets the expectation: this is not
        # a memory test.
        "checks": [{"q": c.get("q"), "options": c.get("options") or [],
                    "kind": c.get("kind"), "from": c.get("from")}
                   for c in (lesson.get("checks") or [])],
        "mode": lesson.get("mode") or "lesson",
        "task": ({"kind": (lesson["task"] or {}).get("kind"),
                  "text": (lesson["task"] or {}).get("text"),
                  "href": (lesson["task"] or {}).get("href")}
                 if lesson.get("task") else None),
        "done": done,
        "unlocked": unlocked,
    }


def course_done(db: Session, user: User, course: str = DEFAULT_COURSE,
                lang: str = DEFAULT_LANG) -> dict | None:
    """The course is completed in full — or `None`.

    There is no separate table for this and none is needed: "completed" is a set of
    `learn_progress` rows, not a state someone sets. Three
    consequences follow, each a plus: it can't be faked, can't be awarded twice, and a race
    between two tabs is impossible by construction.

    For a course with exams (`base`) passing the chapters is not enough: the exams are needed too,
    otherwise the diploma would be awarded for half the work.
    """
    ids = {l.get("id") for l in lessons(course, lang)}
    if not ids:
        return None
    need = set(ids)
    if has_exams(course, lang):
        need |= {exam_id(u.get("id")) for u in content(course, lang).get("units", [])}
    done = _done_ids(db, user)
    if not need <= done:
        return None
    last = db.execute(
        select(func.max(LearnProgress.completed_at))
        .where(LearnProgress.user_id == user.id,
               LearnProgress.lesson_id.in_(need))
    ).scalar()
    data = content(course, lang)
    xp = sum(int(l.get("xp") or 0) for l in lessons(course, lang))
    if has_exams(course, lang):
        xp += EXAM_XP * len(data.get("units", []))
    return {
        "course": course,
        "title": data.get("title", ""),
        "lessons": len(ids),
        "xp": xp,
        "level": level_for(xp, course, lang)["name"],
        "finished_at": last,
    }


def state(db: Session, user: User, course: str = DEFAULT_COURSE,
          lang: str = DEFAULT_LANG) -> dict:
    """Everything the page needs: units, lesson statuses, XP, level.

    Open are the lesson after the last completed one — and all completed ones. There are no
    gaps: a lesson counts only as a whole, and the order is the same for everyone.
    """
    done = _done_ids(db, user)
    data = content(course, lang)
    fin = course_done(db, user, course, lang)
    snippets = data.get("snippets") or {}
    units_out = []
    unlock_next = True          # the first uncompleted lesson of the course is open
    xp = 0
    for unit in data.get("units", []):
        lessons_out = []
        for lesson in unit.get("lessons", []):
            is_done = lesson.get("id") in done
            if is_done:
                xp += int(lesson.get("xp") or 0)
            unlocked = is_done or unlock_next
            if not is_done and unlock_next:
                unlock_next = False
            out = public_lesson(lesson, is_done, unlocked, snippets)
            if out.get("task"):
                out["task"]["optional"] = not task_possible(db, user, out["task"].get("kind"))
            lessons_out.append(out)
        exams_on = has_exams(course, lang)
        exam_done = exams_on and exam_id(unit.get("id")) in done
        all_lessons_done = all(l["done"] for l in lessons_out) and bool(lessons_out)
        if exam_done:
            xp += EXAM_XP
        units_out.append({
            "exam": None if not exams_on else {
                "id": exam_id(unit.get("id")),
                "title": _("Unit test"),
                "xp": EXAM_XP,
                "size": min(EXAM_SIZE, sum(len(l.get("checks") or [])
                                           for l in unit.get("lessons", []))),
                "done": exam_done,
                "unlocked": all_lessons_done,
            },
            "id": unit.get("id"),
            "title": unit.get("title"),
            "subtitle": unit.get("subtitle"),
            "image": unit.get("image"),
            "image_url": image_url(unit["image"]) if unit.get("image") else None,
            "lessons": lessons_out,
            "done": sum(1 for l in lessons_out if l["done"]),
            "total": len(lessons_out),
            "minutes": sum(int(l.get("minutes") or 0) for l in unit.get("lessons", [])),
        })
    return {
        "title": data.get("title", ""),
        "subtitle": data.get("subtitle", ""),
        "course": course,
        "style": data.get("style") or "plain",
        "content_lang": content_lang(course, lang),
        "units": units_out,
        "xp": xp,
        "xp_total": total_xp(course, lang),
        "level": level_for(xp, course, lang),
        "done": len(done & {l.get("id") for l in lessons(course, lang)}),
        "exams_done": sum(1 for u in content(course, lang).get("units", [])
                          if has_exams(course, lang) and exam_id(u.get("id")) in done),
        "lessons_total": len(lessons(course, lang)),
        "minutes_total": sum(int(l.get("minutes") or 0) for l in lessons(course, lang)),
        # "Course completed" used to exist only in the browser — as an inference from
        # there being no open lessons left. The server must know this itself:
        # both the diploma and the special card depend on this fact.
        "completed": bool(fin),
        "finished_at": fin["finished_at"].isoformat(timespec="seconds") if fin else None,
    }


# ---------------------------------------------------------------------------
# Lesson check
# ---------------------------------------------------------------------------

def is_unlocked(db: Session, user: User, lesson_id: str, lang: str = DEFAULT_LANG) -> bool:
    done = _done_ids(db, user)
    course = course_of(lesson_id, lang) or DEFAULT_COURSE
    if is_exam(lesson_id):
        unit = unit_by_id(lesson_id[: -len(EXAM_SUFFIX)], lang)
        if unit is None:
            return False
        return all(l.get("id") in done for l in unit.get("lessons", []))
    for lesson in lessons(course, lang):
        if lesson.get("id") == lesson_id:
            return True                      # first uncompleted — this one
        if lesson.get("id") not in done:
            return False                     # there is an unfinished lesson before it
    return False


def check(db: Session, user: User, lesson_id: str, answers: list[int],
          lang: str = DEFAULT_LANG) -> dict:
    """Answers to a lesson's checks. Returns feedback for each question.

    A lesson is closed only when ALL answers are correct and the task (if any)
    is confirmed. A wrong answer is not a failure: we show the explanation and let
    them try again — the course teaches, it doesn't weed out.
    """
    lesson = lesson_by_id(lesson_id, lang, user_id=user.id)
    if lesson is None:
        raise KeyError(lesson_id)
    checks = lesson.get("checks") or []
    results = []
    for i, c in enumerate(checks):
        picked = answers[i] if i < len(answers) else -1
        correct = picked == int(c.get("answer", -1))
        results.append({
            "correct": correct,
            "answer": int(c.get("answer", -1)),
            "why": c.get("why", ""),
        })
    right = sum(1 for r in results if r["correct"])
    if lesson.get("mode") == "exam":
        need = _pass_mark(len(checks))
        all_correct = right >= need
    else:
        all_correct = bool(results) and all(r["correct"] for r in results)
    task = lesson.get("task") or None
    task_done = True
    task_blocks = False
    if task:
        task_blocks = task_possible(db, user, task.get("kind"))
        task_done = verify_task(db, user, task.get("kind"))
        if not task_blocks:
            task_done = True          # no right — practice is optional
    completed = False
    if all_correct and task_done:
        completed = complete(db, user, lesson)
    return {
        "results": results,
        "right": right,
        "need": _pass_mark(len(checks)) if lesson.get("mode") == "exam" else len(checks),
        "mode": lesson.get("mode") or "lesson",
        "all_correct": all_correct,
        "task_done": task_done,
        "task_hint": (task or {}).get("hint", "") if task and not task_done else "",
        "task_optional": bool(task) and not task_blocks,
        "completed": completed,
    }


def verify_task(db: Session, user: User, kind: Optional[str]) -> bool:
    """A task counts as done only if it is visible in the platform itself."""
    fn = TASKS.get(kind or "")
    if fn is None:
        return True                          # an unknown task doesn't block the course
    try:
        return bool(fn(db, user))
    except Exception:
        return False


def complete(db: Session, user: User, lesson: dict) -> bool:
    """Record completion. True — counted now, False — already existed.

    The second insert is rejected by UNIQUE, and that's not an answer error: two tabs
    that closed the lesson simultaneously both get "completed", XP is awarded once.
    The caller commits (canon: `app/api/favorites.py`)."""
    existing = db.scalar(
        select(LearnProgress).where(LearnProgress.user_id == user.id,
                                    LearnProgress.lesson_id == lesson.get("id")))
    if existing is not None:
        return False
    row = LearnProgress(user_id=user.id, lesson_id=lesson.get("id"),
                        xp=int(lesson.get("xp") or 0))
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    return True
