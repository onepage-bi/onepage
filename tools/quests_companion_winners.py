"""Who has collected the full set and who has the companion. Read-only.

    sudo docker exec -i onepage-onepage-1 python3 - < fox_winners.py

Shows three groups: those whose fox is already unlocked, those who collected 8/8 but have not
visited /quests since (no mark — for the browser the companion does not exist), and
those one step away. The last group is the most useful: it is enough to tell these people
which card they are missing.
"""
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, os.getcwd())
from app.config import settings                    # noqa: E402
from app.database import SessionLocal              # noqa: E402
from app.models import QuestCard, QuestProfile, User  # noqa: E402
from app.services import quests, quests_config     # noqa: E402

quests_config.ensure_fresh()
db = SessionLocal()
# The companion is counted against the season 1 set — that is also the canon for this report.
canon = quests.season_card_ids(quests.COMPANION_SEASON)
N = len(canon)

rows = []
for user, profile in (db.query(User, QuestProfile)
                      .join(QuestProfile, QuestProfile.user_id == User.id)
                      .filter(User.is_active.is_(True)).all()):
    owned = {r[0] for r in db.query(QuestCard.card_id)
             .filter(QuestCard.user_id == user.id, QuestCard.count > 0).all()
             if r[0] in canon}
    rows.append((len(owned), user, profile, sorted(set(canon) - owned)))
rows.sort(key=lambda r: (-r[0], r[1].display_name or ""))

full = [r for r in rows if r[0] >= N]
near = [r for r in rows if r[0] == N - 1]

print("=" * 76)
print("Companion: who got there (companion flag: %s)" % settings.quests_companion_enabled)
print("=" * 76)
print("With a quests profile: %d, of them 8/8: %d, 7/8: %d"
      % (len(rows), len(full), len(near)))

print("\n— FOX UNLOCKED " + "-" * 60)
any_open = False
for n, user, p, _ in full:
    if p.companion_since is None:
        continue
    any_open = True
    print("  %-28s unlocked %s%s%s"
          % (user.display_name or user.person_id,
             p.companion_since.strftime("%d.%m %H:%M"),
             "  TURNED OFF BY USER" if p.companion_off else "",
             "  autumn theme" if p.autumn_theme else ""))
if not any_open:
    print("  nobody")

print("\n— 8/8, BUT NO MARK (has not visited /quests since the eighth) " + "-" * 10)
waiting = [r for r in full if r[2].companion_since is None]
for n, user, p, _ in waiting:
    print("  %-28s the companion unlocks on the first visit to /quests"
          % (user.display_name or user.person_id))
if not waiting:
    print("  nobody")

print("\n— ONE STEP AWAY (7/8) " + "-" * 53)
for n, user, p, missing in near:
    name = quests.card_meta().get(missing[0], {}).get("name", missing[0]) if missing else "?"
    print("  %-28s missing: %s" % (user.display_name or user.person_id, name))
if not near:
    print("  nobody")

print("\n— THE REST, BY CARDS COLLECTED " + "-" * 44)
for n, user, p, _ in rows:
    if n >= N - 1:
        continue
    print("  %-28s %d/%d   points %-5s streak %s"
          % (user.display_name or user.person_id, n, N, p.points, p.streak))
db.close()
