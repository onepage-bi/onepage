"""Quests: the pure core — calendar, streak, card generator.

There is not a single DB call or `request` in here: all this module can do
is compute, from its inputs, which working day it is today, what happened
to the streak and which card was drawn. The routes (phase 3) and the quiz
(phase 4) live separately and call these functions. That way they can be
run by smoke tests without starting the app — like `globmatch` and `semantics`.

Three decisions worth keeping in mind while reading:

* **Lazy computation instead of background jobs.** Neither "a new day has
  begun" nor "has the streak burned out" is computed on a schedule: both are
  a function of `last_open_date` and today's date, evaluated at the moment
  the person opens the page. No cron means no divergence between
  workers (`OP_WORKERS > 1`) and no "catch-up" migrations after downtime.

* **Local zone at the input, naive UTC in the DB.** A "working day" is a
  human notion, and it must be computed in `settings.quests_tz` (default UTC+5,
  no daylight saving time). Hence `now()`/`today()` here, while the
  system "today's date" is forbidden in this file: it would return
  the date by the
  container clock (UTC) and at 3 a.m. local time would open "yesterday's" chest.
  The DB still stores naive-UTC `datetime` values, as everywhere in the
  project; conversion is the caller's job, and the "quest date" is a
  separate `date` column computed by this module.

* **Idempotency via UNIQUE, not via a check.** The functions below
  deliberately do not ask "has he already opened today": `apply_open`
  honestly returns `same_day`, but the real protection against a double click
  and a race between two workers is the UNIQUE index `(user_id, date)` on the opens table.
  The first insert wins, the second catches IntegrityError and reads the existing
  row. A check "before" without UNIQUE would be a race; UNIQUE without the check is
  enough.
"""
from __future__ import annotations

import random
import secrets
from datetime import date, datetime, time, timedelta, timezone

from app.config import settings
from app.i18n import N_, _, _m

# ── Calendar ─────────────────────────────────────────────────────────────

# Fallback zone for the default `quests_tz`, which is UTC+5 all year round
# (no daylight saving), so a fixed offset is a correct substitute
# if tzdata is missing from the image.
_FALLBACK_TZ = timezone(timedelta(hours=5))

_tz_cache: tuple[str, object] | None = None

# Test override of the production calendar: smoke tests do not start the
# app and have no holidays table.
_HOLIDAYS_OVERRIDE: set[date] | None = None

# Test override of "today": smoke tests replay several working days
# in a row and cannot wait a day. Always None in prod.
_TODAY_OVERRIDE: date | None = None


def tz():
    """Quests time zone (cached by the setting's value)."""
    global _tz_cache
    from app.config import settings

    name = getattr(settings, "quests_tz", "Asia/Almaty") or "Asia/Almaty"
    if _tz_cache is not None and _tz_cache[0] == name:
        return _tz_cache[1]
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(name)
    except Exception:
        zone = _FALLBACK_TZ
    _tz_cache = (name, zone)
    return zone


def now() -> datetime:
    """Current time in the quests zone (aware)."""
    return datetime.now(tz())


def today() -> date:
    """Today's date in local time, not by the container clock."""
    if _TODAY_OVERRIDE is not None:
        return _TODAY_OVERRIDE
    return now().date()


def _holidays() -> set[date]:
    """Set of non-working dates. Empty if the reference table does not exist (yet or any more)."""
    if _HOLIDAYS_OVERRIDE is not None:
        return _HOLIDAYS_OVERRIDE
    try:
        from app.services import quests_config

        return quests_config.holidays() or set()
    except Exception:
        return set()


def is_working(d: date) -> bool:
    """Mon–Fri and not a holiday."""
    return d.weekday() < 5 and d not in _holidays()


def prev_working(d: date) -> date:
    """The nearest working day STRICTLY before `d`."""
    cur = d - timedelta(days=1)
    for _step in range(3660):
        if is_working(cur):
            return cur
        cur -= timedelta(days=1)
    raise ValueError("calendar has no working days within 10 years — check the holidays")


def next_working(d: date) -> date:
    """The nearest working day STRICTLY after `d`."""
    cur = d + timedelta(days=1)
    for _step in range(3660):
        if is_working(cur):
            return cur
        cur += timedelta(days=1)
    raise ValueError("calendar has no working days within 10 years — check the holidays")


def working_days_between(a: date, b: date) -> int:
    """Number of working days in the half-open interval (a, b]. 0 if b <= a."""
    if b <= a:
        return 0
    n = 0
    cur = a + timedelta(days=1)
    while cur <= b:
        if is_working(cur):
            n += 1
        cur += timedelta(days=1)
    return n


def working_days_ahead(d: date, n: int) -> list[date]:
    """`d` (if it is a working day) and the following working days — `n` in total."""
    out: list[date] = []
    if n <= 0:
        return out
    cur = d if is_working(d) else next_working(d)
    out.append(cur)
    while len(out) < n:
        cur = next_working(cur)
        out.append(cur)
    return out


def season_start() -> date:
    """Season start date from settings; today if the value is malformed."""
    from app.config import settings

    raw = getattr(settings, "quests_season_start", "") or ""
    try:
        start = date.fromisoformat(str(raw).strip()[:10])
    except Exception:
        return today()
    return start if is_working(start) else next_working(start)


def season_start_utc() -> datetime:
    """Midnight of the season's first day in local time, converted to naive
    UTC — the boundary before which neither the audit nor the tables give rewards."""
    local = datetime.combine(season_start(), time.min, tzinfo=tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def season_day_index(d: date) -> int:
    """Index of the season's working day, starting at 0 (also 0 before the start)."""
    start = season_start()
    count = working_days_between(start, d) + (1 if is_working(start) else 0)
    return max(0, count - 1)


QUIZ_CYCLE_DEFAULT = 15


def quiz_bank_day(d: date, cycle: int | None = None) -> int:
    """Question bank of the day: 1..cycle, cycling over the season's working days.

    The cycle length is set by the bank ITSELF (the highest day number among active
    questions), not by a constant: otherwise every bank top-up would require a
    code change, and nobody would top it up. The caller passes it from the DB —
    here, in a calendar function, we make no queries.
    """
    n = int(cycle or QUIZ_CYCLE_DEFAULT)
    if n < 1:
        n = QUIZ_CYCLE_DEFAULT
    return season_day_index(d) % n + 1


def iso_week_key(d: date) -> str:
    """ISO week key, `2026-W37` — used to mark a spent freeze."""
    y, w, _dow = d.isocalendar()
    return f"{y}-W{w:02d}"


# ── Streak ───────────────────────────────────────────────────────────────

# One missed day per week is covered by a "freeze": people get sick and travel,
# and losing a two-month streak over one day is an insult, not motivation.
# Two in a row is no longer an accident; the streak is honestly reset.

def apply_open(
    last_open_date: date | None,
    streak: int,
    freeze_week: str | None,
    d: date,
) -> tuple[int, str | None, str]:
    """Recompute the streak when the chest is opened on day `d`.

    Returns `(new_streak, new_freeze_week, event)`, where the event is one of
    `same_day` / `first` / `continued` / `frozen` / `reset`.
    """
    if last_open_date is not None and last_open_date == d:
        return streak, freeze_week, "same_day"
    if last_open_date is None:
        return 1, freeze_week, "first"

    if d < last_open_date:
        return streak, freeze_week, "same_day"  # clock went backwards — leave the streak alone
    gap = working_days_between(last_open_date, d) - 1  # missed working days
    if gap < 0:
        # Opening on a weekend or holiday (no working days between last and d):
        # the card is issued, the streak does not move — it is about working days.
        return streak, freeze_week, "same_day"
    if gap == 0:
        return streak + 1, freeze_week, "continued"
    # The freeze is charged to the week of the MISSED day, not the opening day:
    # Friday W37 → Monday W38 spends the W37 freeze.
    week = iso_week_key(prev_working(d))
    if gap == 1 and freeze_week != week:
        return streak + 1, week, "frozen"
    return 1, freeze_week, "reset"


def milestones(streak_after: int) -> dict:
    """Round-streak bonuses: every 3rd doubles the rarity, every 7th adds an extra surprise."""
    return {
        "rare_x2": streak_after % 3 == 0,
        "extra_surprise": streak_after % 7 == 0,
    }


def reward_path(d: date, streak_now: int, opened_today: bool) -> list[dict]:
    """Reward track: today and the next 6 working days."""
    days = working_days_ahead(d, 7)
    working = is_working(d)
    # On a weekend the track starts at the nearest working day, and it is NOT
    # "today": otherwise Saturday labelled Monday as today.
    base = streak_now if (opened_today and working) else streak_now + 1
    out: list[dict] = []
    for offset, day in enumerate(days):
        streak_n = base + offset
        m = milestones(streak_n)
        out.append({
            "day": day,
            "offset": offset,
            "streak_n": streak_n,
            "rare_x2": m["rare_x2"],
            "extra": m["extra_surprise"],
            "today": offset == 0 and working,
            "done": offset == 0 and working and opened_today,
        })
    return out


# ── Cards, seasons and the draw ──────────────────────────────────────────
#
# The collection lives in SEASONS. Season 1 (foxes) is frozen: its cards already sit
# in people's `quest_cards` and stay there forever, but nothing is drawn from it
# any more. The active season is the one used for the draw, duplicate exchange,
# and whose odds are shown in the UI.
#
# Three rules everything rests on:
#
# * **`card_id` is unique ACROSS seasons** (season 2 uses the `frog-` prefix),
#   so `quest_cards` needs no "season" column: the season number is always
#   derived from the id itself via the registry below. There is no migration and never will be.
# * **Every "how many collected" count is filtered by the season's set.** Without
#   the filter four foxes plus four frogs would read as "8 of 8" —
#   see `owned_count_db`.
# * **The active season switches without a redeploy** — via the `quests_season` field in
#   the `data/quests-runtime.json` overlay (`quests_config`). The `active` flag in
#   the registry is only the default when there is no overlay.

# (id, rarity, base weight). The base weights sum to 100, so a base
# weight reads as a percentage at zero streak. Weights and rarities are IDENTICAL
# across seasons — otherwise season statistics would not be comparable.
_SEASON1_CARDS: list[tuple[str, str, int]] = [
    ("watch", "common", 22),
    ("chat", "common", 20),
    ("build", "common", 18),
    ("talk", "common", 15),
    ("guard", "rare", 10),
    ("streak", "rare", 8),
    ("gold", "epic", 5),
    ("star", "legendary", 2),
]

# `N_` and not `_`: the dict is built at import time, when no request — and so
# no language — exists. The translation happens in `card_meta()` below, once per
# response, in the language of the person who asked.
_SEASON1_META: dict[str, dict] = {
    "watch": {
        "name": N_("Fox the Observer"),
        "rarity": "common",
        "desc": N_("First to notice when a chart has gone sideways."),
    },
    "chat": {
        "name": N_("Fox the Conversationalist"),
        "rarity": "common",
        "desc": N_("Asks «Explore» instead of flipping through a hundred tabs."),
    },
    "build": {
        "name": N_("Fox the Builder"),
        "rarity": "common",
        "desc": N_("Assembles dashboards from building blocks while others are still writing specs."),
    },
    "talk": {
        "name": N_("Fox the Presenter"),
        "rarity": "common",
        "desc": N_("Three slides, one idea, zero questions from the floor."),
    },
    "guard": {
        "name": N_("Fox the Keeper"),
        "rarity": "rare",
        "desc": N_("Keeps Data Quality in the green zone."),
    },
    "streak": {
        "name": N_("Fox the Deadline"),
        "rarity": "rare",
        "desc": N_("Delivers everything on time. Logs in every working day, even on Mondays."),
    },
    "gold": {
        "name": N_("Golden Fox"),
        "rarity": "epic",
        "desc": N_("Drops rarely. Safe to show off to colleagues."),
    },
    "star": {
        "name": N_("Fox the Stargazer"),
        "rarity": "legendary",
        "desc": N_("One in a hundred surprises. Caught a star at the top."),
    },
}

# Season 2 — frogs. Roles (owner's decision): commentator — annotations on
# the report, neat freak — Anonymizer, why-asker — report chat, cartographer —
# reference tables, auditor — folder check before a run, apprentice —
# Practicum, golden frog, samurai frog. Rarities and weights exactly as in
# season 1, position by position.
_SEASON2_CARDS: list[tuple[str, str, int]] = [
    ("frog-note", "common", 22),
    ("frog-clean", "common", 20),
    ("frog-why", "common", 18),
    ("frog-map", "common", 15),
    ("frog-audit", "rare", 10),
    ("frog-learn", "rare", 8),
    ("frog-gold", "epic", 5),
    ("frog-samurai", "legendary", 2),
]

# TODO(season 2): names and descriptions are PLACEHOLDERS. Final texts are written
# by a separate contributor; each card's role is fixed by the owner and
# must not change, only the wording changes. Keys (`card_id`),
# rarities and weights must not be touched — they are already in the DB and in the statistics.
_SEASON2_META: dict[str, dict] = {
    "frog-note": {
        "name": N_("Annotator toad"),
        "rarity": "common",
        "desc": N_("Writes a note on the report itself, before the thought cools down."),
    },
    "frog-clean": {
        "name": N_("Tidy toad"),
        "rarity": "common",
        "desc": N_("Runs the export through the Anonymizer, then shows it to the AI. Not the other way round."),
    },
    "frog-why": {
        "name": N_("Questioning toad"),
        "rarity": "common",
        "desc": N_("Asks the report a question in the chat. Excel gets the day off."),
    },
    "frog-map": {
        "name": N_("Cartographer toad"),
        "rarity": "common",
        "desc": N_("Keeps the reference tables. Her codes and names always find each other."),
    },
    "frog-audit": {
        "name": N_("Auditor toad"),
        "rarity": "rare",
        "desc": N_("Checks the data folder before the generator runs, not after."),
    },
    "frog-learn": {
        "name": N_("Student toad"),
        "rarity": "rare",
        "desc": N_("Finishes the course to the last step — including the one everybody skips."),
    },
    "frog-gold": {
        "name": N_("Golden toad"),
        "rarity": "epic",
        "desc": N_("Rarely drawn. Looks splendid next to the golden fox."),
    },
    "frog-samurai": {
        "name": N_("Samurai toad"),
        "rarity": "legendary",
        "desc": N_("Arrived when the fox had stopped waiting. Bowed, and took her place beside him."),
    },
}

# Ordered season registry. `active` is the DEFAULT (when the
# overlay has no `quests_season`); the live decision is made by `active_season()`.
SEASONS: list[dict] = [
    {
        "key": 1,
        "title": N_("Season 1 — foxes"),
        "cards": _SEASON1_CARDS,
        "meta": _SEASON1_META,
        # Active until the frog art lands on disk: a card takes its image from
        # /static/img/quests/<card_id>.jpg, and a season without art would hand out
        # broken images. Switching is done via the overlay, without a redeploy.
        "active": True,
    },
    {
        "key": 2,
        "title": N_("Season 2 — toads"),
        "cards": _SEASON2_CARDS,
        "meta": _SEASON2_META,
        "active": False,
    },
]

SEASON_BY_KEY: dict[int, dict] = {s["key"]: s for s in SEASONS}

# The companion (ADR-17b) is permanently tied to the full SEASON 1 set: whoever collected
# the foxes keeps the companion, and frogs do not affect it at all.
COMPANION_SEASON = 1

# Season returned when the overlay names a non-existent number.
_DEFAULT_SEASON = next((s for s in SEASONS if s["active"]), SEASONS[-1])


def active_season() -> dict:
    """The season the draw comes from — per the `quests_season` overlay.

    Read via `quests_config.ensure_fresh()` (TTL 1 s + mtime), so
    a season switch in the admin panel reaches all workers on its own, without
    a redeploy or a restart.
    """
    try:
        from app.services import quests_config

        quests_config.ensure_fresh()
    except Exception:  # pragma: no cover — the overlay must not break the draw
        pass
    try:
        key = int(getattr(settings, "quests_season", 0) or 0)
    except (TypeError, ValueError):
        key = 0
    return SEASON_BY_KEY.get(key) or _DEFAULT_SEASON


def season(key: int | None = None) -> dict:
    """Season by number; `None` means the active one."""
    if key is None:
        return active_season()
    return SEASON_BY_KEY.get(int(key)) or _DEFAULT_SEASON


def active_cards() -> list[tuple[str, str, int]]:
    """The active season's set — what is being drawn right now."""
    return active_season()["cards"]


def season_card_ids(key: int | None = None) -> list[str]:
    """`card_id`s of the season's set (`None` — active) in registry order."""
    return [cid for cid, _r, _w in season(key)["cards"]]


def companion_cards() -> list[tuple[str, str, int]]:
    """The set that earns the companion: always season 1."""
    return season(COMPANION_SEASON)["cards"]


def all_card_meta() -> dict[str, dict]:
    """Raw meta of ALL seasons in one dict — for showing an already issued
    card: an open from season 1 must keep its name even after
    season 2 has become active."""
    out: dict[str, dict] = {}
    for s in SEASONS:
        for cid, meta in s["meta"].items():
            out[cid] = {**meta, "season": s["key"]}
    return out


def card_meta() -> dict[str, dict]:
    """Card meta in the current request's language — across ALL seasons.

    Deliberately not just the active season: the client uses this dict to
    label both today's openings and the history, where the foxes live. It is
    also the `card_meta` key in `state()`.

    A fresh dict every time and not a cached one per language: it is a
    couple of dozen small entries built once per response, and a cache keyed
    on the language would have to be invalidated whenever the catalogue is
    reloaded."""
    return {cid: {**meta, "name": _m(meta["name"]), "desc": _m(meta["desc"])}
            for cid, meta in all_card_meta().items()}


def season_card_list(key: int | None = None) -> list[dict]:
    """A season's set as a list of dicts — a ready piece of the `state()` response."""
    meta = season(key)["meta"]
    return [{"card_id": cid, "rarity": rarity, "base_weight": weight,
             "name": _m(meta.get(cid, {}).get("name", cid)),
             "desc": _m(meta.get(cid, {}).get("desc", ""))}
            for cid, rarity, weight in season(key)["cards"]]


def __getattr__(name: str):
    """`CARDS` and `CARD_META` are kept as names but mean the ACTIVE season.

    Via `__getattr__` (PEP 562) rather than plain constants: the active season
    is switched at runtime by the overlay, and a constant would freeze at its
    import-time value. The names are kept because smoke tests, the admin panel
    and `tools/quests_*` look at them — and there is no ambiguity: "CARDS" no
    longer exists in the module at all, internally everything goes through
    `active_cards()`, and the season 1 set is called `companion_cards()`.
    """
    if name == "CARDS":
        return active_cards()
    if name == "CARD_META":
        return active_season()["meta"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_BOOSTED = ("rare", "epic", "legendary")


def weights(streak: int, rare_x2: bool = False) -> list[float]:
    """Card weights: the streak raises the chance of everything rarer than common.

    The cap is streak 10 (+100 % to rare ones), otherwise by the end of the season
    common cards would stop dropping altogether.
    """
    boost = 1.0 + 0.1 * min(max(int(streak or 0), 0), 10)
    if rare_x2:
        boost *= 2.0
    return [float(w) * boost if rarity in _BOOSTED else float(w)
            for _cid, rarity, w in active_cards()]


def draw(streak: int, *, rare_x2: bool = False, seed: str | None = None) -> dict:
    """Draw a card. The same `seed` gives the same result."""
    if seed is None:
        seed = secrets.token_hex(8)
    cards = active_cards()
    ws = weights(streak, rare_x2)
    total = sum(ws)
    rnd = random.Random(seed)
    roll = min(rnd.random() * total, total * (1 - 1e-12))
    acc = 0.0
    card_id, rarity = cards[0][0], cards[0][1]  # guard against accumulated float error
    for (cid, rar, _w), w in zip(cards, ws):
        acc += w
        if roll < acc:
            card_id, rarity = cid, rar
            break
    return {
        "card_id": card_id,
        "rarity": rarity,
        "seed": seed,
        "weights": ws,
        "roll": roll,
    }


def chances(streak: int, rare_x2: bool = False) -> list[dict]:
    """The same weights, but as percentages — for the UI hint and the admin table."""
    ws = weights(streak, rare_x2)
    total = sum(ws) or 1.0
    return [{"card_id": cid, "pct": w * 100.0 / total}
            for (cid, _r, _bw), w in zip(active_cards(), ws)]


LEVELS: list[tuple[int, str]] = [
    (0, N_("Novice")),
    (8, N_("Practitioner")),
    (18, N_("Confident user")),
    (30, N_("Expert")),
]


def level(quiz_correct_total: int) -> str:
    """Rank by the all-time number of correct quiz answers — already translated."""
    n = int(quiz_correct_total or 0)
    name = LEVELS[0][1]
    for threshold, title in LEVELS:
        if n >= threshold:
            name = title
    return _m(name)


def short_name(display_name: str | None, person_id: str) -> str:
    """"Surname I." for the leaderboard: the colleague's name, but not the full name."""
    parts = (display_name or "").split()
    if not parts:
        return "—"  # the personnel number is not shown on the shared board
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0]}."


# ── Quest verification (phase 3) ─────────────────────────────────────────
#
# This is where the part of the module that talks to the DB begins. Three rules shared
# by everything below:
#
# * **We commit nothing.** The route (`app/api/quests.py`) owns the transaction,
#   as in `favorites.py`: the service only adds rows and changes the profile.
#   That way "award a quest" and "write the audit" reach the DB in one transaction.
# * **Idempotency via UNIQUE, not SELECT.** Every insert that must happen
#   at most once runs inside
#   `with db.begin_nested()` (SAVEPOINT): the race loser catches
#   `IntegrityError`, rolls back only the savepoint and returns "already there".
#   The outer transaction stays alive — unlike a bare
#   `db.rollback()`, which would also wipe everything the caller had accumulated.
# * **A quest reward is not "the day's opening".** A quest surprise draws
#   a card but does NOT touch the streak (`apply_open`): the streak is about the daily
#   chest, otherwise five quests on the first day would give a streak of 5.

# Same rule as `CARD_META`: `N_` here, `_` where `state()` builds the response.
QUESTS: list[dict] = [
    {
        "id": "pres",
        "title": N_("Assemble your first presentation from two dashboards"),
        "points": 20,
        "surprise": True,
        "how": "pres",
        "go": "/presentations",
        "src": ("presentation_create", "presentation_item_add"),
    },
    {
        "id": "fav",
        "title": N_("Add a report to favourites"),
        "points": 10,
        "surprise": True,
        "how": "fav",
        "go": "/",
        "src": ("favorite.add",),
    },
    {
        "id": "theme",
        "title": N_("Change the theme via Ctrl+K"),
        "points": 10,
        "surprise": False,
        "how": "theme",
        "go": "/",
        "src": (),
        "client_kind": "theme",
    },
    {
        "id": "grid",
        "title": N_("Switch the report list from table to grid"),
        "points": 10,
        "surprise": False,
        "how": "grid",
        "go": "/",
        "src": (),
        "client_kind": "view",
    },
    {
        "id": "share",
        "title": N_("Create a link to your report via Share"),
        "points": 15,
        "surprise": True,
        "how": "share",
        "go": "/",
        "src": ("share_link_create",),
    },
]

QUESTS_BY_ID: dict[str, dict] = {q["id"]: q for q in QUESTS}

# Audit actions that can complete a quest. Only these go into the
# incremental scan — verification does not care about the rest of the audit.
WATCHED_ACTIONS = (
    "favorite.add",
    "presentation_create",
    "presentation_item_add",
    "share_link_create",
)

# Client event kind → the quest it completes.
CLIENT_EVENT_QUESTS: dict[str, str] = {
    q["client_kind"]: q["id"] for q in QUESTS if q.get("client_kind")
}

_SCAN_LIMIT = 500


class AlreadyOpened(Exception):
    """Today's chest is already open (or a neighbouring tab opened it)."""


class NotWorkingDay(AlreadyOpened):
    """Weekend or holiday: there is no daily surprise today."""


def _utcnow() -> datetime:
    """Naive UTC — the way the rest of the DB stores it."""
    return datetime.utcnow()


# Scan cursor overlap. `AuditLog.created_at` is filled by the DB (`func.now()`),
# and SQLite returns `CURRENT_TIMESTAMP` with one-second precision — no fractional
# part, whereas a Python `datetime` goes into the query parameter as
# `...:53.000000`. With string comparison in SQLite "10:42:53" < "10:42:53.000000",
# so an action taken in the same second as the previous scan
# would fall through the gap forever. Hence every scan starts one second
# before the cursor: re-checking other rows is cheap (rewards are idempotent),
# losing a row is not.
_SCAN_OVERLAP = timedelta(seconds=1)


def _log(db, actor, action: str, resource_type: str, resource_id: str,
         detail: dict, ip: str = "") -> None:
    """Canon `app/api/favorites.py:21-28`: we add the row, the caller commits."""
    from app.models import AuditLog

    entry = AuditLog(
        actor_id=actor.id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        ip_address=ip,
    )
    entry.set_detail(detail)
    db.add(entry)


def ensure_profile(db, user):
    """The user's quest profile; created on first access.

    The insert runs in a SAVEPOINT: `user_id` is the primary key, and two
    concurrent requests from one person (page + header badge,
    two workers) would otherwise crash the whole request with an IntegrityError instead
    of reading the row the neighbour has just created.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import QuestProfile

    profile = db.query(QuestProfile).filter(QuestProfile.user_id == user.id).first()
    if profile is not None:
        return profile
    profile = QuestProfile(user_id=user.id, points=0, streak=0, best_streak=0)
    try:
        with db.begin_nested():
            db.add(profile)
            db.flush()
    except IntegrityError:
        if profile in db:  # savepoint rollback may already have dropped it
            db.expunge(profile)
        profile = db.query(QuestProfile).filter(QuestProfile.user_id == user.id).first()
        if profile is None:  # pragma: no cover - the row vanished between insert and read
            raise
    return profile


def opening_json(opening) -> dict | None:
    """An opening in a form fit for both a JSON response and `data-state`."""
    if opening is None:
        return None
    meta = all_card_meta().get(opening.card_id) or SPECIAL_BY_CARD.get(opening.card_id, {})
    # Translated here, at the only door every card leaves through: the daily
    # chest, the quiz prize, a milestone and an exchange all come back to the
    # browser as this dict.
    return {
        "id": opening.id,
        "source": opening.source,
        "card_id": opening.card_id,
        "rarity": opening.rarity,
        "name": _m(meta["name"]) if meta.get("name") else opening.card_id,
        "desc": _m(meta["desc"]) if meta.get("desc") else "",
        "day": opening.day.isoformat() if opening.day else None,
        "created_at": opening.created_at.isoformat() if opening.created_at else None,
        "revealed": opening.revealed_at is not None,
    }


def pending_openings(db, user_id: int) -> list:
    """Cards that have already been drawn (quest, quiz, milestone, exchange) but
    the person has not yet "opened" in the header. Oldest first."""
    from app.models import QuestOpening

    return (
        db.query(QuestOpening)
        .filter(QuestOpening.user_id == user_id, QuestOpening.revealed_at.is_(None))
        .order_by(QuestOpening.created_at, QuestOpening.id)
        .all()
    )


def pending_special(db, user_id: int):
    """The pending SPECIAL card (course completion reward), if there is one."""
    for op in pending_openings(db, user_id):
        if op.rarity == "special":
            return op
    return None


def reveal_special(db, user_id: int):
    """Open the course reward. Returns an opening or None."""
    op = pending_special(db, user_id)
    if op is None:
        return None
    op.revealed_at = _utcnow()
    db.flush()
    return op


def grant_season_opener(db, user, profile, *, ip: str = ""):
    """A season switch must not cost a person their day.

    Whoever managed to open today's surprise BEFORE the switch got
    a card from the previous season — and would be left without the first card of the new one.
    Such a person gets a pending one: they open it themselves, in the same capsule
    as the course reward (owner, 05.10: "let those who already opened the
    fox surprise today also get a froglet").

    The condition deliberately looks at the FACT, not the switch date: we issue it
    only if there has already been an opening today and none of today's cards
    is from the active season. So issuance is limited to exactly the switch day,
    needs nothing remembered and will not fire twice — `UNIQUE(user_id,
    day, source)` and the "this source already exists" check also close the race
    of two tabs.

    Returns an opening or None. Does not commit.
    """
    from app.models import QuestOpening

    key = active_season()["key"]
    source = "season:%d" % key
    seen = db.query(QuestOpening.id).filter(
        QuestOpening.user_id == profile.user_id, QuestOpening.source == source,
    ).first()
    if seen is not None:
        return None

    d = today()
    todays = db.query(QuestOpening.card_id).filter(
        QuestOpening.user_id == profile.user_id, QuestOpening.day == d,
    ).all()
    if not todays:
        return None                      # has not opened today — will get a new-season card the usual way
    ids = set(season_card_ids(key))
    if any(c[0] in ids for c in todays):
        return None                      # already has a new-season card, nothing to catch up

    op = grant_opening(db, profile.user_id, d, source, int(profile.streak or 0))
    if op is not None:
        _log(db, user, "quests_season_opener", "quest_profile", str(profile.user_id),
             {"season": key, "card_id": op.card_id}, ip=ip)
    return op


def reveal_pending(db, profile):
    """Open the oldest pending surprise. Returns an opening or None."""
    items = pending_openings(db, profile.user_id)
    if not items:
        return None
    op = items[0]
    op.revealed_at = _utcnow()
    db.flush()
    return op


def grant_opening(db, user_id: int, day: date, source: str,
                  streak_for_weights: int, *, rare_x2: bool = False):
    """Draw a card and record the opening. `None` if one already exists.

    UNIQUE(user_id, day, source) is the only protection against a double click and
    a race between two workers: the insert runs in a SAVEPOINT, the loser gets
    `None` and does not award a second card. Does not commit.
    """
    import json as _json

    from sqlalchemy.exc import IntegrityError

    from app.models import QuestCard, QuestOpening

    res = draw(int(streak_for_weights or 0), rare_x2=rare_x2)
    stamp = _utcnow()
    opening = QuestOpening(
        user_id=user_id,
        day=day,
        source=source,
        card_id=res["card_id"],
        rarity=res["rarity"],
        seed=res["seed"],
        weights_json=_json.dumps([round(w, 4) for w in res["weights"]]),
        roll=float(res["roll"]),
        created_at=stamp,
        revealed_at=stamp if source == "daily" else None,  # everything but daily waits to be opened
    )
    try:
        with db.begin_nested():
            db.add(opening)
            db.flush()
    except IntegrityError:
        return None

    card = db.query(QuestCard).filter(
        QuestCard.user_id == user_id, QuestCard.card_id == res["card_id"],
    ).first()
    if card is None:
        try:
            with db.begin_nested():
                card = QuestCard(user_id=user_id, card_id=res["card_id"], count=1,
                                 first_at=stamp, last_at=stamp)
                db.add(card)
                db.flush()
        except IntegrityError:
            # A neighbouring tab created the same collection row a second earlier.
            card = db.query(QuestCard).filter(
                QuestCard.user_id == user_id, QuestCard.card_id == res["card_id"],
            ).first()
            if card is not None:
                card.count = int(card.count or 0) + 1
                card.last_at = stamp
    else:
        card.count = int(card.count or 0) + 1
        card.last_at = stamp
    db.flush()
    return opening


def award_quest(db, profile, quest_id: str, evidence: dict) -> dict | None:
    """Complete a quest exactly once. `None` — it was completed earlier.

    Points are awarded only together with a successful `quest_completions` insert;
    the surprise (if the quest is a "surprise" one) is drawn right here, but does not
    touch the streak — see the section header.
    """
    import json as _json

    from sqlalchemy.exc import IntegrityError

    from app.models import QuestCompletion

    spec = QUESTS_BY_ID.get(quest_id)
    if spec is None:
        raise ValueError(_("Unknown quest: {quest_id}", quest_id=quest_id))

    row = QuestCompletion(
        user_id=profile.user_id,
        quest_id=quest_id,
        completed_at=_utcnow(),
        points=int(spec["points"]),
        surprise=bool(spec["surprise"]),
    )
    row.evidence_json = _json.dumps(evidence or {}, ensure_ascii=False, default=str)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None

    profile.points = int(profile.points or 0) + int(spec["points"])
    opening = None
    if spec["surprise"]:
        opening = grant_opening(db, profile.user_id, today(),
                                f"quest:{quest_id}", int(profile.streak or 0))
    return {
        "quest_id": quest_id,
        "title": _m(spec["title"]) if spec.get("title") else quest_id,
        "points": int(spec["points"]),
        "surprise": bool(spec["surprise"]),
        "opening": opening_json(opening),
    }


def completed_ids(db, user_id: int) -> set[str]:
    """Ids of already completed quests."""
    from app.models import QuestCompletion

    return {r[0] for r in db.query(QuestCompletion.quest_id)
            .filter(QuestCompletion.user_id == user_id).all()}


def _pres_done(db, user_id: int, presentation_id: str | None = None,
               since: datetime | None = None) -> str | None:
    """id of the user's presentation that has ≥2 dashboard slides.
    `since` cuts off presentations created before the season start."""
    from sqlalchemy import func as sa_func

    from app.models import PRES_DASHBOARD, Presentation, PresentationItem

    q = (
        db.query(PresentationItem.presentation_id)
        .join(Presentation, Presentation.id == PresentationItem.presentation_id)
        .filter(Presentation.owner_id == user_id,
                PresentationItem.kind == PRES_DASHBOARD)
    )
    if presentation_id:
        q = q.filter(PresentationItem.presentation_id == presentation_id)
    if since is not None:
        q = q.filter(Presentation.created_at >= since)
    row = (
        q.group_by(PresentationItem.presentation_id)
        .having(sa_func.count(PresentationItem.id) >= 2)
        .first()
    )
    return row[0] if row else None


def _fav_done(db, user_id: int) -> tuple[bool, str | None]:
    """`(has a favourite, dashboard_id)`. A table row or a trace in the audit:
    old-timers may have removed the ★, but the fact stayed in the log."""
    from app.models import AuditLog, DashboardFavorite

    row = db.query(DashboardFavorite.dashboard_id).filter(
        DashboardFavorite.user_id == user_id).first()
    if row:
        return True, row[0]
    row = db.query(AuditLog.resource_id).filter(
        AuditLog.actor_id == user_id, AuditLog.action == "favorite.add").first()
    return (True, row[0]) if row else (False, None)


def _share_done(db, user_id: int) -> tuple[bool, str | None]:
    """`(has created a share link, dashboard_id)`. The token may have been revoked —
    then the quest is confirmed by a `share_link_create` audit record."""
    from app.models import AuditLog, ShareToken

    row = db.query(ShareToken.dashboard_id).filter(
        ShareToken.created_by == user_id).first()
    if row:
        return True, row[0]
    row = db.query(AuditLog.resource_id).filter(
        AuditLog.actor_id == user_id, AuditLog.action == "share_link_create").first()
    return (True, row[0]) if row else (False, None)


def verify(db, user, profile) -> list[dict]:
    """Award everything the person did SINCE THE SEASON START. Does not commit.

    Owner's decision (2026-09-04): actions before the campaign start do not count —
    neither by table state nor by audit. The **first visit** only sets the
    `profile.verified_at` cursor to midnight of the season's first day and writes
    `quests_backfill` (with `since`, no rewards); after that the regular
    scan works. Historical credit from tables (the first version) was removed.

    The **scan** is incremental, over `audit_log` from the
    `profile.verified_at` cursor (but not earlier than `season_start_utc()`) for four
    actions, at most 500 rows per call. The cursor moves to the
    `created_at` of the last row seen: if there are exactly 500 rows,
    the next call continues from the same place.
    """
    awards: list[dict] = []
    done = completed_ids(db, user.id)
    since = season_start_utc()

    if not profile.backfilled:
        profile.backfilled = True
        profile.verified_at = since
        _log(db, user, "quests_backfill", "user", str(user.id),
             {"awarded": [], "since": since.isoformat(timespec="seconds"),
              "policy": "season_only"})

    from app.models import AuditLog

    # Cursor no earlier than the season start. A cursor "from the future" (the season start
    # was scheduled ahead and then moved to today) is reset to the
    # start — otherwise the season's actions would stay behind the cursor forever.
    cur = profile.verified_at or _utcnow()
    if cur > _utcnow():
        cur = since
    cursor = max(cur - _SCAN_OVERLAP, since)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.actor_id == user.id,
                AuditLog.created_at >= cursor,
                AuditLog.action.in_(WATCHED_ACTIONS))
        .order_by(AuditLog.created_at, AuditLog.id)
        .limit(_SCAN_LIMIT)
        .all()
    )
    if not rows:
        return awards

    recheck_pres = False
    for row in rows:
        quest_id: str | None = None
        evidence: dict = {"via": "audit", "audit_id": row.id,
                          "action": row.action, "resource_id": row.resource_id}
        if row.action == "favorite.add":
            quest_id = "fav"
        elif row.action == "share_link_create":
            quest_id = "share"
        elif row.action == "presentation_item_add":
            # A slide was added — but the quest is completed only by the SECOND dashboard in
            # one presentation, so the condition is re-checked against the table.
            if _pres_done(db, user.id, row.resource_id, since=since):
                quest_id = "pres"
            else:
                recheck_pres = True
        elif row.action == "presentation_create":
            # An empty presentation completes nothing; it is only a reason
            # to re-check the whole condition (slides may have been added earlier).
            recheck_pres = True
        if not quest_id or quest_id in done:
            continue
        award = award_quest(db, profile, quest_id, evidence)
        if award:
            awards.append(award)
            done.add(quest_id)

    if recheck_pres and "pres" not in done:
        pres_id = _pres_done(db, user.id, since=since)
        if pres_id:
            award = award_quest(db, profile, "pres",
                                {"via": "audit", "presentation_id": pres_id})
            if award:
                awards.append(award)
                done.add("pres")

    last = rows[-1].created_at
    if last is not None and (profile.verified_at is None or last > profile.verified_at):
        profile.verified_at = last
    return awards


def record_client_event(db, profile, kind: str) -> dict:
    """Client event (theme / grid): once per user.

    Such events are confirmed only by the browser, so they give points but
    never a surprise (owner's decision). UNIQUE(user_id, kind) holds the
    ceiling: a repeat returns `{already: True}` and awards nothing.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import QuestClientEvent

    quest_id = CLIENT_EVENT_QUESTS.get(kind)
    if quest_id is None:
        raise ValueError(_("Unknown client event: {kind}", kind=kind))

    try:
        with db.begin_nested():
            db.add(QuestClientEvent(user_id=profile.user_id, kind=kind,
                                    created_at=_utcnow()))
            db.flush()
    except IntegrityError:
        return {"already": True, "award": None}

    award = award_quest(db, profile, quest_id, {"via": "client_event", "kind": kind})
    return {"already": False, "award": award}


def exchange_duplicates(db, profile, card_id: str) -> dict:
    """Five identical cards → one extra opening.

    The debit is a conditional UPDATE (`count >= 5`), not "read, check,
    write": two tabs pressing "exchange" at the same time would read
    the same five and each get a card, and the counter would go
    negative. The winner is the one whose UPDATE touched the row (`rowcount == 1`);
    the loser gets the ordinary `ValueError` "five cards needed".

    Both the debit and the opening insert are held by ONE savepoint: the `dup:n` number
    is computed after the debit, and if a neighbour has already taken that number
    (UNIQUE(user_id, day, source)), everything rolls back — the cards stay with
    the person, and `AlreadyOpened` goes out (409 in the route). There is
    deliberately no retry on `n`: repeating the request is cheaper than a loop that would
    fail the same way in a race.
    """
    from sqlalchemy import func as sa_func, update

    from app.models import QuestCard, QuestOpening

    # Exchange only within the active season: duplicates of the frozen season 1
    # stay with the person as they are, otherwise a completed set could be
    # traded for a card of another season.
    if card_id not in season_card_ids():
        raise ValueError(_("This card cannot be traded: the season is closed"))

    d = today()
    with db.begin_nested():
        res = db.execute(
            update(QuestCard)
            .where(QuestCard.user_id == profile.user_id,
                   QuestCard.card_id == card_id,
                   QuestCard.count >= 5)
            .values(count=QuestCard.count - 5)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:
            raise ValueError(_("Five matching cards are needed"))
        # `synchronize_session=False` left the ORM object with the old `count`,
        # and `grant_opening` below may increment exactly this row — re-read
        # it, otherwise the increment would land on top of the undebited five.
        db.query(QuestCard).populate_existing().filter(
            QuestCard.user_id == profile.user_id, QuestCard.card_id == card_id,
        ).first()

        used = db.query(sa_func.count(QuestOpening.id)).filter(
            QuestOpening.user_id == profile.user_id,
            QuestOpening.day == d,
            QuestOpening.source.like("dup:%"),
        ).scalar() or 0

        opening = grant_opening(db, profile.user_id, d, f"dup:{used + 1}",
                                int(profile.streak or 0))
        if opening is None:
            raise AlreadyOpened(_("The exchange wasn't recorded, try again"))

    card = db.query(QuestCard).populate_existing().filter(
        QuestCard.user_id == profile.user_id, QuestCard.card_id == card_id,
    ).first()
    return {"opening": opening_json(opening),
            "remaining": int(card.count or 0) if card is not None else 0}


def open_daily(db, profile) -> dict:
    """Daily chest: streak, card and, on a milestone, a second card."""
    from app.models import QuestOpening

    d = today()
    if not is_working(d):
        # Owner's rule: the daily surprise is on working days only. On a weekend
        # or holiday there is no chest at all, rather than "there is one, but without a streak".
        raise NotWorkingDay(
            _("Today is a day off — the surprise of the day is waiting for the next working day"))
    exists = db.query(QuestOpening.id).filter(
        QuestOpening.user_id == profile.user_id,
        QuestOpening.day == d,
        QuestOpening.source == "daily",
    ).first()
    if exists:
        raise AlreadyOpened(_("Today's chest has already been opened"))

    new_streak, freeze_week, event = apply_open(
        profile.last_open_date, int(profile.streak or 0), profile.freeze_week, d)
    ms = milestones(new_streak)

    opening = grant_opening(db, profile.user_id, d, "daily", new_streak,
                            rare_x2=ms["rare_x2"])
    if opening is None:
        # A neighbouring tab got there first — there will be no second card.
        raise AlreadyOpened(_("Today's chest has already been opened"))

    profile.streak = new_streak
    profile.best_streak = max(int(profile.best_streak or 0), new_streak)
    profile.freeze_week = freeze_week
    profile.last_open_date = d

    extra = None
    if ms["extra_surprise"]:
        extra = grant_opening(db, profile.user_id, d, "milestone", new_streak)

    return {
        "opening": opening_json(opening),
        "extra_opening": opening_json(extra),
        "streak": new_streak,
        "event": event,
        "milestones": ms,
    }


def state(db, user, profile) -> dict:
    """Everything the `/quests` page needs, in one JSON-serialisable dict.

    Today's openings are returned as a whole list: the schema has no "shown"
    column, and adding one for the sake of an animation is not worth it — the client
    remembers in localStorage which cards it has already played.

    SEASON CONTRACT (for the client, which is edited separately).

    The old keys are kept and do not change shape:

    * `cards`: {card_id: count} — the ACTIVE season's set, exactly eight
      keys. This is what the draw adds to and what duplicate exchange uses.
    * `card_meta`: {card_id: {name, desc, rarity, season}} — meta across ALL
      seasons (a superset of the old dict), so both a fox from the
      history and today's frog can be labelled. The `season` field was added.
    * `chances`: [{card_id, pct}] — the active season only, as before.
    * `companion`: as before, but computed from the full SEASON 1 set
      and not dependent on frogs at all.

    New key:

    * `seasons`: [{key, title, active, companion, total, owned,
                   cards: [{card_id, rarity, base_weight, name, desc}],
                   counts: {card_id: count}}]
      in `SEASONS` registry order. `active` — which season the draw
      comes from; `companion` — which season earns the companion (season 1);
      `owned` — how many cards of THIS season are collected, `total` — how many
      the set has. The client must compute "collected N of M" from this key,
      not from the length of `card_meta`.
    * `active_season`: the active season number (a duplicate for convenience).
    """
    from app.models import QuestCard, QuestCompletion, QuestOpening

    d = today()
    openings = (
        db.query(QuestOpening)
        .filter(QuestOpening.user_id == user.id, QuestOpening.day == d)
        .order_by(QuestOpening.created_at, QuestOpening.id)
        .all()
    )
    working_today = is_working(d)
    # On a weekend there is no daily chest, and an old opening (from before the "working
    # days only" rule) must not turn Saturday into "already opened today".
    opened_today = working_today and any(o.source == "daily" for o in openings)

    # `cards` is the ACTIVE season: the collection the draw adds to
    # and that duplicate exchange uses. The full per-season breakdown is below, in
    # the `seasons` key.
    owned_by_season: dict[int, dict[str, int]] = {
        s["key"]: {cid: 0 for cid, _r, _w in s["cards"]} for s in SEASONS
    }
    for row in db.query(QuestCard).filter(QuestCard.user_id == user.id).all():
        for counts in owned_by_season.values():
            if row.card_id in counts:
                counts[row.card_id] = int(row.count or 0)
    cards = owned_by_season[active_season()["key"]]

    done = {
        r.quest_id: r for r in
        db.query(QuestCompletion).filter(QuestCompletion.user_id == user.id).all()
    }
    quests = []
    for spec in QUESTS:
        row = done.get(spec["id"])
        quests.append({
            "id": spec["id"],
            "title": _m(spec["title"]),
            "points": spec["points"],
            "surprise": bool(spec["surprise"]),
            "done": row is not None,
            "completed_at": row.completed_at.isoformat() if row is not None and row.completed_at else None,
            "how": spec["how"],
            "go": spec["go"],
        })

    streak_now = int(profile.streak or 0)
    path = [dict(step, day=step["day"].isoformat())
            for step in reward_path(d, streak_now, opened_today)]

    return {
        "today": d.isoformat(),
        "working_today": working_today,
        "next_working": next_working(d).isoformat(),
        "opened_today": opened_today,
        "openings_today": [opening_json(o) for o in openings],
        "pending": [opening_json(o) for o in pending_openings(db, profile.user_id)],
        "streak": streak_now,
        "best_streak": int(profile.best_streak or 0),
        "freeze_available": profile.freeze_week != iso_week_key(d),
        "path": path,
        "points": int(profile.points or 0),
        "level": level(profile.quiz_correct_total),
        "cards": cards,
        "card_meta": card_meta(),
        "active_season": active_season()["key"],
        "seasons": [
            {
                "key": s["key"],
                "title": _m(s["title"]),
                "active": s["key"] == active_season()["key"],
                "companion": s["key"] == COMPANION_SEASON,
                "total": len(s["cards"]),
                "owned": owned_count(owned_by_season[s["key"]]),
                "cards": season_card_list(s["key"]),
                "counts": owned_by_season[s["key"]],
            }
            for s in SEASONS
        ],
        # A separate key, not inside `cards`: the eight-card set and the companion are counted
        # from `cards`, and a special card must not get into that number.
        "special": special_cards(db, user),
        # The companion is for the full SEASON 1 set, not the active season:
        # otherwise whoever collected the foxes would lose it the day season 2 starts.
        "companion": companion_info(profile, owned_count(owned_by_season[COMPANION_SEASON])),
        "quests": quests,
        "chances": chances(streak_now if (opened_today or not working_today) else streak_now + 1),
        "quiz_correct_total": int(profile.quiz_correct_total or 0),
        "leaderboard_optin": bool(profile.leaderboard_optin),
    }


# ---------------------------------------------------------------------------
# Special cards: an achievement for completing a Practicum course
# ---------------------------------------------------------------------------
# Two cards, DELIBERATELY outside the eight-card set and its mechanics: they are not drawn,
# not exchanged, have no rarity and do not bring the 8/8 companion closer. Owning
# them is computed from `learn_progress` and stored nowhere — such a card
# cannot be "issued" by mistake, twice, or bypassing the course.
SPECIAL_CARDS: list[dict] = [
    {"id": "learn-prep", "course": "prep",
     "name": N_("The Analyst Fox"),
     "desc": N_("Made it to the end of “The First Report” — and now answers with a number, not an opinion.")},
    {"id": "learn-base", "course": "base",
     "name": N_("The Graduate Fox"),
     "desc": N_("Finished the base course on working with the platform: knows how a report is built and where trust in a number comes from.")},
]


SPECIAL_BY_CARD: dict[str, dict] = {
    "learn-" + spec["course"]: spec for spec in SPECIAL_CARDS
}


def special_art(card_id: str) -> str:
    return "/static/img/quests/special/%s.jpg" % card_id


def special_for_course(course: str) -> dict | None:
    """The card this course gives — for the Practicum final screen."""
    for spec in SPECIAL_CARDS:
        if spec["course"] == course:
            return {"id": spec["id"], "name": _m(spec["name"]),
                    "desc": _m(spec["desc"]), "art": special_art(spec["id"])}
    return None


def grant_special_opening(db, user_id: int, course: str):
    """Put a special card into the surprise queue — not issue it, but present it.

    Ownership of the card is still COMPUTED from the completed course; this
    row exists for the ceremony: until it is revealed, the platform keeps
    the capsule on screen, and the person opens it in the same place as the daily
    surprise. So `quest_cards` is not touched here at all: the eight-card set,
    duplicate exchange and the 8/8 companion condition are counted from it and stay
    exactly the same.

    UNIQUE(user_id, day, source) guards against a second row; the insert runs in a
    SAVEPOINT, as in `grant_opening`. Does not commit.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import QuestOpening

    card_id = "learn-" + course
    if card_id not in SPECIAL_BY_CARD:
        return None
    # Already presented (on any day) — there is no second capsule.
    seen = db.query(QuestOpening.id).filter(
        QuestOpening.user_id == user_id,
        QuestOpening.card_id == card_id,
    ).first()
    if seen is not None:
        return None
    stamp = _utcnow()
    opening = QuestOpening(
        user_id=user_id, day=today(), source="learn:" + course,
        card_id=card_id, rarity="special", seed="", weights_json="[]",
        roll=0.0, created_at=stamp, revealed_at=None,
    )
    try:
        with db.begin_nested():
            db.add(opening)
            db.flush()
    except IntegrityError:
        return None
    return opening


def special_cards(db, user) -> list[dict]:
    """The person's special cards: one for each completed Practicum course."""
    from app.services import learn

    out = []
    for spec in SPECIAL_CARDS:
        fin = None
        try:
            fin = learn.course_done(db, user, spec["course"])
        except Exception:
            fin = None                      # a course without content is no reason to crash
        out.append({
            "id": spec["id"],
            "course": spec["course"],
            "name": _m(spec["name"]),
            "desc": _m(spec["desc"]),
            "art": special_art(spec["id"]),
            "course_title": (fin or {}).get("title") or "",
            "owned": fin is not None,
            "since": fin["finished_at"].isoformat() if fin and fin.get("finished_at") else None,
        })
    return out


# ---------------------------------------------------------------------------
# ADR-17b "Companion": the creature behind the full collection
# ---------------------------------------------------------------------------

def owned_count(cards: dict) -> int:
    return sum(1 for v in (cards or {}).values() if int(v or 0) > 0)


def owned_count_db(db, user_id: int, season_key: int | None = None) -> int:
    """How many DIFFERENT cards of the given season are collected.

    The filter by the season's set is mandatory: without it four foxes and four frogs
    would give "8 of 8" and a companion for an incomplete set. The default is the
    companion's season (season 1), because every call to this function today is
    about the companion.
    """
    from app.models import QuestCard

    ids = season_card_ids(COMPANION_SEASON if season_key is None else season_key)
    return int(db.query(QuestCard.card_id).filter(
        QuestCard.user_id == user_id, QuestCard.count > 0,
        QuestCard.card_id.in_(ids),
    ).distinct().count())


def companion_seed(user_id: int) -> str:
    """Stable per-person seed: the client derives the creature's small
    variations and its appearance schedule from it. Season-bound, so a new
    season is a new creature."""
    import hashlib
    raw = f"{int(user_id)}:{season_start().isoformat()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def companion_info(profile, owned: int) -> dict | None:
    """What the page and the badge carry about the creature — None until the
    collection is complete or while the administrator has it switched off.
    Read-only: stamping `companion_since` is `companion_touch`'s job."""
    if owned < len(companion_cards()) or not settings.quests_companion_enabled:
        return None
    return {
        "seed": companion_seed(profile.user_id),
        "since": profile.companion_since.isoformat() if profile.companion_since else None,
        "enabled": not bool(profile.companion_off),
        "autumn": bool(profile.autumn_theme),
    }


def season_reward_info(db, profile, season_key: int) -> dict | None:
    """The reward for a COMPLETE season set — a single entry point for the future.

    Season 1 → companion (ADR-17b). For season 2 the reward has NOT been chosen
    by the owner yet, hence a deliberate `None` here: when the decision comes, it
    is added as a branch HERE, and neither `companion_info` nor anything else in
    the season 1 mechanics will need touching.
    """
    if int(season_key) == COMPANION_SEASON:
        return companion_info(
            profile, owned_count_db(db, profile.user_id, COMPANION_SEASON))
    # TODO(season 2): the reward for eight frogs is undecided — a placeholder, not a forgotten
    # branch. Do not invent anything here on your own.
    return None


def companion_touch(db, user, profile) -> bool:
    """Stamp the moment the collection became complete — once. Called on
    the mutating paths only (page render, POST /open); a GET never writes.
    Returns True when this call did the stamping, i.e. the intro is due."""
    if profile.companion_since is not None or not settings.quests_companion_enabled:
        return False
    if owned_count_db(db, profile.user_id) < len(companion_cards()):
        return False
    profile.companion_since = datetime.utcnow()
    _log(db, user, "quests_companion_reveal", "quest_profile", str(profile.user_id),
         {"seed": companion_seed(profile.user_id)})
    return True


def set_companion(db, user, profile, *, enabled: bool | None = None,
                  autumn: bool | None = None, ip: str = "") -> dict:
    """The person's own two switches, from the Collection block. Refused
    (PermissionError) before the collection is complete: the switches are
    part of the reward, not a setting anyone has."""
    if (owned_count_db(db, profile.user_id) < len(companion_cards())
            or not settings.quests_companion_enabled):
        raise PermissionError("companion not unlocked")
    changed: dict = {}
    if enabled is not None:
        profile.companion_off = not bool(enabled)
        changed["enabled"] = bool(enabled)
    if autumn is not None:
        profile.autumn_theme = bool(autumn)
        changed["autumn"] = bool(autumn)
    if changed:
        _log(db, user, "quests_companion_set", "quest_profile", str(profile.user_id), changed, ip=ip)
    return companion_info(profile, len(companion_cards()))


def leaderboard(db, user, profile) -> dict:
    """Percentile always, the table only with consent.

    By default a person sees only "you are ahead of N % of colleagues": publishing
    the name and points is an explicit opt-in (owner's decision). The department is taken from
    the HR directory with a separate query by `person_id`, not a JOIN: `employees`
    can hold more than one row per person, and a JOIN would multiply
    the leaderboard rows.
    """
    from sqlalchemy import func as sa_func

    from app.models import Employee, QuestProfile, User

    my_points = int(profile.points or 0)
    total = db.query(sa_func.count(QuestProfile.user_id)).scalar() or 0
    below = db.query(sa_func.count(QuestProfile.user_id)).filter(
        QuestProfile.points < my_points).scalar() or 0
    percentile = int(round(below * 100.0 / total)) if total else 0

    median = 0
    if total:
        row = (
            db.query(QuestProfile.points)
            .order_by(QuestProfile.points)
            .offset(total // 2).limit(1).first()
        )
        median = int(row[0]) if row else 0

    out = {"percentile": percentile, "total": int(total), "median": median}
    if not profile.leaderboard_optin:
        return out

    ahead = db.query(sa_func.count(QuestProfile.user_id)).filter(
        QuestProfile.leaderboard_optin.is_(True),
        QuestProfile.points > my_points).scalar() or 0
    out["me_rank"] = int(ahead) + 1

    top = (
        db.query(QuestProfile, User)
        .join(User, User.id == QuestProfile.user_id)
        .filter(QuestProfile.leaderboard_optin.is_(True))
        .order_by(QuestProfile.points.desc(), QuestProfile.user_id)
        .limit(20)
        .all()
    )
    ids = [u.person_id for _p, u in top if u.person_id]
    depts: dict[str, str] = {}
    if ids:
        for oid, dept, active in db.query(
            Employee.person_id, Employee.deptname_ru, Employee.is_active,
        ).filter(Employee.person_id.in_(ids)).all():
            if not oid or not dept:
                continue
            if oid not in depts or active:
                depts[oid] = dept

    out["rows"] = [
        {
            "rank": i + 1,
            "name": short_name(u.display_name, u.person_id),
            "dept": depts.get(u.person_id),
            "points": int(p.points or 0),
            "me": p.user_id == user.id,
        }
        for i, (p, u) in enumerate(top)
    ]
    return out


# ── Quiz (phase 4) ───────────────────────────────────────────────────────

def quiz_question(*args, **kwargs):  # pragma: no cover - TODO phase 4
    raise NotImplementedError("Quiz — phase 4")


def quiz_answer(*args, **kwargs):  # pragma: no cover - TODO phase 4
    raise NotImplementedError("Quiz — phase 4")
