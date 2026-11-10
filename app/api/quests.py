"""Quests (ADR-17) API — phase 5: thin routing over `app/services/quests.py`
and `app/services/quests_quiz.py`. This module owns the transaction (canon
`app/api/favorites.py`): services only add/change rows, `db.commit()` is
always here, after a successful `_log`.

Two routers in one file:

* `router` — user-facing `/quests*` and `/api/quests/*`, all under
  `Depends(_require_quests_enabled)`: while the feature is off (`OP_QUESTS_ENABLED`
  or the admin overlay), they all 404 at once, like `/explore` (canon
  `app/api/explore.py:55-61`).
* `admin_router` — `/admin/quests` and `/api/admin/quests/*`, WITHOUT that
  guard: an administrator must be able to reach the switch and turn the
  feature on, not get a 404 on their own panel while it is off.

Both are mounted in `app/main.py` next to the other routers.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.i18n import _
from app.auth.deps import get_current_user, require_permission
from app.config import settings
from app.database import get_db
from app.models import QuestOpening, User
from app.services import quests, quests_config, quests_quiz, quests_stats


def _require_quests_enabled() -> None:
    # `ensure_fresh` re-reads the admin overlay (≤1 s TTL) so a flag flipped
    # by another worker reaches this request without a restart — same
    # pattern as `runtime_config`/`quests_config` everywhere else.
    quests_config.ensure_fresh()
    if not settings.quests_enabled:
        raise HTTPException(status_code=404, detail="Quests is disabled")


router = APIRouter(dependencies=[Depends(_require_quests_enabled)])
admin_router = APIRouter()


def _log(db: Session, actor: User, action: str, resource_type: str, resource_id: str,
         detail: dict, ip: str = "") -> None:
    """Canon `app/api/favorites.py:21-28`. Adds the row; the caller commits."""
    from app.models import AuditLog

    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ─────────────────────────────── shared state ──────────────────────────────


def _full_state(db: Session, user: User, *, mutate: bool) -> dict:
    """Everything both the `/quests` page and `/api/quests/state` need: the base
    `quests.state()` plus the leaderboard, quiz status and the current season
    config.

    `mutate=True` is the page-render path: it also verifies uncredited
    quests and lazily seeds the question bank. `mutate=False` is the
    `GET /api/quests/state` path: a PURE READ. A GET that grants awards and
    commits is both an extra write on every badge poll and an award
    handed out in response to a request the browser is free to repeat or
    prefetch. Awards now go through `POST /api/quests/verify` and
    `POST /api/quests/open`. Does not commit in either mode — the commit is
    the caller's."""
    profile = quests.ensure_profile(db, user)
    awards: list[dict] = []
    if mutate:
        awards = quests.verify(db, user, profile)
        quests_quiz.seed_default_bank(db)  # empty bank → built-in 45; no-op afterwards
        quests.companion_touch(db, user, profile)  # ADR-17b: stamp once at 8/8
        # Season rollover: whoever already opened a surprise from the previous
        # season today gets a deferred card of the new one. Written only here,
        # on the /quests render — the hot path and the badge are left alone.
        # No IP passed here: `_full_state` is also called from places with no
        # request object. The audit records who got what anyway.
        quests.grant_season_opener(db, user, profile)
    st = quests.state(db, user, profile)
    st["leaderboard"] = quests.leaderboard(db, user, profile)
    st["quiz"] = quests_quiz.status(db, profile)
    st["awards"] = awards
    cfg = quests_config.get()
    st["config"] = {
        "season_start": cfg["season_start"],
        "season_length": cfg["season_length"],
        "tz": settings.quests_tz,
    }
    return st


@router.get("/quests", response_class=HTMLResponse)
async def quests_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        st = _full_state(db, user, mutate=True)
        db.commit()
        return st

    st = await run_in_threadpool(_run)
    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/quests.html").render,
        user=user, request=request,
        state_json=json.dumps(st, ensure_ascii=False, default=str),
    )
    return HTMLResponse(html_out)


@router.get("/api/quests/state")
async def api_quests_state(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pure read: no `verify()`, no bank seeding, no `commit()`.

    Awards arrive via `POST /api/quests/verify` (the page calls it
    after actions) and via `POST /api/quests/open`; `awards` here is always
    an empty list — the key is kept so the client need not tell state
    sources apart."""
    def _run() -> dict:
        return _full_state(db, user, mutate=False)

    return await run_in_threadpool(_run)


@router.post("/api/quests/verify")
async def api_quests_verify(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Settle outstanding quests and return the granted awards.

    Exactly the work `GET /api/quests/state` used to do: the page
    calls this POST after an action ("opened a report", "starred one") and
    reads the state separately."""
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        awards = quests.verify(db, user, profile)
        db.commit()
        return {"awards": awards}

    return await run_in_threadpool(_run)


@router.get("/api/quests/badge")
async def api_quests_badge(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cheap check for the header badge: ONE query, no quest verification
    and no profile creation — a profile is not needed to know whether
    today's chest has been opened."""
    def _run() -> dict:
        d = quests.today()
        exists = db.query(QuestOpening.id).filter(
            QuestOpening.user_id == user.id,
            QuestOpening.day == d,
            QuestOpening.source == "daily",
        ).first()
        # Second cheap query: unopened awards (quest, quiz, milestone,
        # exchange) also keep the egg on screen — otherwise the person never
        # learns their surprise is waiting. `open` = "nothing to show".
        pending = db.query(QuestOpening.id).filter(
            QuestOpening.user_id == user.id,
            QuestOpening.revealed_at.is_(None),
        ).count()
        # On a day off there is no daily chest — only deferred awards keep the capsule.
        closed = bool(exists) or not quests.is_working(d)
        out = {"open": closed and pending == 0, "pending": int(pending), "date": d.isoformat()}
        # ADR-17b: the creature rides the same once-a-day answer — no new
        # request on any page. One COUNT(DISTINCT) more, only here.
        if settings.quests_companion_enabled:
            from app.models import QuestProfile
            prof = db.get(QuestProfile, user.id)
            # `companion_since` is stamped on the /quests visit where the 8th
            # card arrived — before that visit there is nothing to show on any
            # other page, so everyone else pays one primary-key read here.
            if prof is not None and prof.companion_since is not None:
                comp = quests.companion_info(prof, quests.owned_count_db(db, user.id))
                if comp:
                    out["companion"] = comp
        return out

    return await run_in_threadpool(_run)


def _log_opening(db: Session, user: User, opening: dict | None, ip: str) -> None:
    """An audit row for EVERY card granted.

    `opening_json` (what the service returns) deliberately omits seed/roll/
    weights — they exist for investigating a disputed draw, not for the client.
    The audit needs them, so the row is re-read rather than widening the
    service's public shape. A milestone (`source=milestone`) is logged the same
    way: without this, the second card on day 5/10/20 left no trace, and
    there was nothing to answer "where did that legendary come from" with."""
    if opening is None:
        return
    row = db.query(QuestOpening).filter(QuestOpening.id == opening["id"]).first()
    weights = None
    if row is not None and row.weights_json:
        try:
            weights = json.loads(row.weights_json)
        except (ValueError, TypeError):
            weights = None
    _log(db, user, "quests_open", "quest_opening", str(opening["id"]), {
        "source": opening.get("source"),
        "card_id": opening.get("card_id"),
        "rarity": opening.get("rarity"),
        "seed": row.seed if row is not None else None,
        "roll": row.roll if row is not None else None,
        "weights": weights,
    }, ip=ip)


@router.post("/api/quests/open")
async def api_quests_open(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Open today's chest.

    Starts with `quests.verify()`: `GET /api/quests/state` no longer grants
    anything (pure read), and a returning person often lands not on the
    quests page but straight on the FAB — without this call their
    yesterday's achievements would wait for the next visit to `/quests`.
    Awards are committed together with the opening."""
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        quests.verify(db, user, profile)
        # The course reward goes AHEAD of the daily chest. The person was sent here
        # by the Practicum's final screen promising a special card; handing out an
        # ordinary daily one instead breaks the promise at exactly the moment
        # they came for. The daily chest is not lost: it
        # stays closed and waits for the next click.
        special = quests.reveal_special(db, profile.user_id)
        if special is not None:
            db.commit()
            return {"kind": "pending", "opening": quests.opening_json(special),
                    "extra_opening": None, "streak": profile.streak,
                    "pending_left": len(quests.pending_openings(db, profile.user_id))}
        try:
            result = quests.open_daily(db, profile)
            quests.companion_touch(db, user, profile)  # ADR-17b
            result["kind"] = "daily"
        except quests.AlreadyOpened as exc:
            # The daily one is already open (or today is a day off) — hand out the
            # next deferred surprise (quest, quiz, milestone, exchange). The card
            # was drawn and audited when it was granted; here it is
            # only "opened".
            pending = quests.reveal_pending(db, profile)
            if pending is None:
                detail = (str(exc) if isinstance(exc, quests.NotWorkingDay)
                          else _("Today's surprise has already been opened"))
                raise HTTPException(status_code=409, detail=detail)
            db.commit()
            return {"kind": "pending", "opening": quests.opening_json(pending),
                    "extra_opening": None, "streak": profile.streak,
                    "pending_left": len(quests.pending_openings(db, profile.user_id))}

        ip = _client_ip(request)
        _log_opening(db, user, result.get("opening"), ip)
        _log_opening(db, user, result.get("extra_opening"), ip)
        db.commit()
        result["pending_left"] = len(quests.pending_openings(db, profile.user_id))
        return result

    return await run_in_threadpool(_run)


class ExchangeBody(BaseModel):
    card_id: str


@router.post("/api/quests/exchange")
async def api_quests_exchange(
    body: ExchangeBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        try:
            result = quests.exchange_duplicates(db, profile, body.card_id)
        except quests.AlreadyOpened as exc:
            # A sibling tab took the `dup:n` number — no cards were spent.
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _log(db, user, "quests_exchange", "quest_card", body.card_id,
             {"remaining": result["remaining"]}, ip=_client_ip(request))
        db.commit()
        return result

    return await run_in_threadpool(_run)


class EventBody(BaseModel):
    kind: Literal["theme", "view"]


@router.post("/api/quests/event")
async def api_quests_event(
    body: EventBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        result = quests.record_client_event(db, profile, body.kind)
        _log(db, user, "quests_client_event", "quest", body.kind,
             {"already": result.get("already")}, ip=_client_ip(request))
        db.commit()
        return result

    return await run_in_threadpool(_run)


@router.post("/api/quests/quiz/start")
async def api_quests_quiz_start(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start (or resume) today's attempt. Does NOT start the timer.

    The response is either `{token, day, index, total, question, opponent, score,
    served, remaining_sec}` or `{done: true, summary}` if the
    resume's auto-timeout finished off the last question: the client must
    show the summary, not a 409 out of nowhere (the 409 comes on the next
    start). The stopwatch starts with a separate `POST …/quiz/begin`."""
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        quests_quiz.seed_default_bank(db)  # lazy: no-op once the bank exists
        ip = _client_ip(request)
        try:
            result = quests_quiz.start(db, profile, ip=ip)
        except quests_quiz.AlreadyPlayed:
            raise HTTPException(status_code=409, detail=_("Today's quiz already completed"))
        except quests_quiz.QuizClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except quests_quiz.QuizUnavailable:
            raise HTTPException(status_code=503, detail=_("Questions for today not loaded"))
        if not result.get("done"):
            _log(db, user, "quests_quiz_start", "quiz_attempt", result["token"][:8],
                 {"bank_day": result["day"]}, ip=ip)
        db.commit()
        return result

    return await run_in_threadpool(_run)


class QuizBeginBody(BaseModel):
    token: str


@router.post("/api/quests/quiz/begin")
async def api_quests_quiz_begin(
    body: QuizBeginBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start the timer of the current question — idempotent.

    A separate step after `start()`, because an intro screen sits between the
    server's response and the moment the person actually sees the question:
    stamping in `start()` would gift the seconds burned on it to the opponent.
    A repeated call does not extend the time — the real remainder comes back."""
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        try:
            result = quests_quiz.begin(db, profile, body.token)
        except quests_quiz.QuizNotFound:
            raise HTTPException(status_code=404, detail=_("Attempt not found"))
        except quests_quiz.AlreadyPlayed:
            raise HTTPException(status_code=409, detail=_("Today's quiz already completed"))
        except quests_quiz.QuizClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except quests_quiz.QuizUnavailable:
            raise HTTPException(status_code=503, detail=_("Questions for today not loaded"))
        db.commit()
        return result

    return await run_in_threadpool(_run)


class QuizAnswerBody(BaseModel):
    token: str
    pick: Optional[int] = None
    index: int


@router.post("/api/quests/quiz/answer")
async def api_quests_quiz_answer(
    body: QuizAnswerBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        ip = _client_ip(request)
        try:
            result = quests_quiz.answer(db, profile, body.token, body.pick, body.index,
                                        ip=ip)
        except quests_quiz.QuizNotFound:
            raise HTTPException(status_code=404, detail=_("Attempt not found"))
        except quests_quiz.QuizStale:
            raise HTTPException(status_code=409, detail=_("Question already answered"))
        except quests_quiz.AlreadyPlayed:
            raise HTTPException(status_code=409, detail=_("Today's quiz already completed"))
        except quests_quiz.QuizClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except quests_quiz.QuizUnavailable:
            raise HTTPException(status_code=503, detail=_("Questions for today not loaded"))
        # One row per answer: the audit used to hold only the finish, and a dispute
        # "my second question wasn't counted" had nothing to go on. The question
        # text is deliberately absent — only the index, the outcome and the time.
        _log(db, user, "quests_quiz_answer", "quiz_attempt", str(body.token)[:8], {
            "index": body.index,
            "correct": bool(result.get("correct")),
            "pts": int(result.get("points") or 0),
            "elapsed_ms": int(result.get("elapsed_ms") or 0),
        }, ip=ip)
        db.commit()
        return result

    return await run_in_threadpool(_run)


class OptinBody(BaseModel):
    optin: bool


@router.put("/api/quests/leaderboard-optin")
async def api_quests_leaderboard_optin(
    body: OptinBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        profile.leaderboard_optin = bool(body.optin)
        profile.optin_at = datetime.utcnow()
        _log(db, user, "quests_optin", "quest_profile", str(user.id),
             {"optin": body.optin}, ip=_client_ip(request))
        db.commit()
        return quests.leaderboard(db, user, profile)

    return await run_in_threadpool(_run)


class CompanionBody(BaseModel):
    enabled: Optional[bool] = None
    autumn: Optional[bool] = None


@router.put("/api/quests/companion")
async def api_quests_companion(
    body: CompanionBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """ADR-17b: the two switches in the Collection block — show the creature,
    use the autumn theme. 403 until the collection is complete: the switches
    are part of the reward."""
    def _run() -> dict:
        profile = quests.ensure_profile(db, user)
        try:
            info = quests.set_companion(db, user, profile, enabled=body.enabled,
                                        autumn=body.autumn, ip=_client_ip(request))
        except PermissionError:
            raise HTTPException(status_code=403, detail=_("The companion appears only with a full collection"))
        db.commit()
        return {"ok": True, "companion": info}

    return await run_in_threadpool(_run)


# ─────────────────────────────────── admin ──────────────────────────────────


@admin_router.get("/admin/quests", response_class=HTMLResponse)
async def admin_quests_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    def _run():
        cfg = quests_config.get()
        bank = quests_quiz.bank_status(db)
        # Cards of the ACTIVE season: the odds table in the admin page describes
        # what is being drawn now. The full opening stats below cover
        # all seasons — season 1 cards have their own history that must not be lost.
        meta = quests.card_meta()
        cards = [
            {
                "card_id": cid,
                "name": meta.get(cid, {}).get("name", cid),
                "rarity": rarity,
                "base_weight": weight,
            }
            for cid, rarity, weight in quests.active_cards()
        ]
        stats_rows = (
            db.query(QuestOpening.card_id, func.count(QuestOpening.id))
            .group_by(QuestOpening.card_id)
            .all()
        )
        stats = {cid: int(n) for cid, n in stats_rows}
        return cfg, bank, cards, stats

    cfg, bank, cards, stats = await run_in_threadpool(_run)
    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/admin_quests.html").render,
        user=user, request=request, config=cfg, bank=bank, cards=cards, stats=stats,
    )
    return HTMLResponse(html_out)


@admin_router.get("/admin/quests/stats", response_class=HTMLResponse)
async def admin_quests_stats_page(
    request: Request,
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    """Who opened a surprise, who scored how much, retention. Read-only."""
    data = await run_in_threadpool(quests_stats.collect, db)
    env: Environment = request.app.state.jinja
    html_out = await run_in_threadpool(
        env.get_template("pages/admin_quests_stats.html").render,
        user=user, request=request, s=data,
    )
    return HTMLResponse(html_out)


@admin_router.get("/api/admin/quests/stats")
async def api_admin_quests_stats(
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    data = await run_in_threadpool(quests_stats.collect, db)
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


@admin_router.get("/api/admin/quests/config")
async def api_admin_quests_config_get(
    user: User = Depends(require_permission("view_admin")),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        cfg = quests_config.get()
        cfg["bank"] = quests_quiz.bank_status(db)
        return cfg

    return await run_in_threadpool(_run)


class AdminConfigBody(BaseModel):
    enabled: Optional[bool] = None
    companion_enabled: Optional[bool] = None
    season_start: Optional[str] = None
    season_length: Optional[int] = None
    holidays: Optional[list[str]] = None
    reset: Optional[list[str]] = None


@admin_router.put("/api/admin/quests/config")
async def api_admin_quests_config_put(
    body: AdminConfigBody,
    request: Request,
    user: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        changes: dict = {}
        if body.enabled is not None:
            changes["quests_enabled"] = body.enabled
        if body.companion_enabled is not None:
            changes["quests_companion_enabled"] = body.companion_enabled
        if body.season_start is not None:
            changes["quests_season_start"] = body.season_start
        if body.season_length is not None:
            changes["quests_season_length"] = body.season_length
        try:
            touched = quests_config.set_overrides(
                changes, holidays=body.holidays, reset=body.reset or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Names only, never values (canon: quests_config.set_overrides docstring).
        _log(db, user, "quests_admin_config", "quests_config", "quests",
             {"changed": touched}, ip=_client_ip(request))
        db.commit()
        return quests_config.get()

    return await run_in_threadpool(_run)


class QuizImportBody(BaseModel):
    bank: list
    dry_run: bool = False


@admin_router.post("/api/admin/quests/quiz-import")
async def api_admin_quests_quiz_import(
    body: QuizImportBody,
    request: Request,
    user: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        report = quests_quiz.import_bank(db, body.bank, dry_run=body.dry_run)
        if not report.get("ok"):
            # The full report, not just `errors`: the admin JS needs both
            # `detail.count` and `detail.days` to show "0 of 45".
            raise HTTPException(status_code=422, detail=report)
        if not body.dry_run:
            _log(db, user, "quests_quiz_import", "quiz_bank", "default",
                 {"count": report.get("count")}, ip=_client_ip(request))
            db.commit()
        return report

    return await run_in_threadpool(_run)


class QuizImportDefaultBody(BaseModel):
    dry_run: bool = False


@admin_router.post("/api/admin/quests/quiz-import-default")
async def api_admin_quests_quiz_import_default(
    body: QuizImportDefaultBody,
    request: Request,
    user: User = Depends(require_permission("manage_users")),
    db: Session = Depends(get_db),
):
    def _run() -> dict:
        bank = quests_quiz.load_default_bank()
        report = quests_quiz.import_bank(db, bank, dry_run=body.dry_run)
        if not report.get("ok"):
            # The full report, not just `errors`: the admin JS needs both
            # `detail.count` and `detail.days` to show "0 of 45".
            raise HTTPException(status_code=422, detail=report)
        if not body.dry_run:
            _log(db, user, "quests_quiz_import", "quiz_bank", "default",
                 {"count": report.get("count")}, ip=_client_ip(request))
            db.commit()
        return report

    return await run_in_threadpool(_run)
