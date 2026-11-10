"""Revoke awards granted for actions BEFORE the season start (the historical credit
of the first verification version, cancelled by the owner's decision of 2026-09-04).

What it does for every `quest_completions` row with `evidence.via == "backfill"`:
deducts the points, deletes the same day's `quest:<id>` opening (and decrements
the card counter in the collection), deletes the completion itself; sets the
`verified_at` cursor to the season start — on the next visit, actions
done within the season will be credited again from the audit log.

Run inside the container (dry run by default):
    python tools/quests_revoke_backfill.py            # report only
    python tools/quests_revoke_backfill.py --apply    # apply
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import AuditLog, QuestCard, QuestCompletion, QuestOpening, QuestProfile, User
from app.services import quests as q


def main(apply: bool) -> int:
    db = SessionLocal()
    since = q.season_start_utc()
    touched = 0
    try:
        rows = db.query(QuestCompletion).all()
        for c in rows:
            try:
                ev = json.loads(c.evidence_json or "{}")
            except Exception:
                ev = {}
            if ev.get("via") != "backfill":
                continue
            profile = db.get(QuestProfile, c.user_id)
            day = c.completed_at.date() if c.completed_at else None
            opening = db.query(QuestOpening).filter(
                QuestOpening.user_id == c.user_id,
                QuestOpening.source == f"quest:{c.quest_id}").order_by(QuestOpening.id.desc()).first()
            u = db.get(User, c.user_id)
            who = f"{u.person_id} ({u.display_name or '—'})" if u else f"user#{c.user_id}"
            print(f"{who}: quest={c.quest_id} points=-{c.points} "
                  f"opening={'card ' + opening.card_id if opening else 'none'} day={day}")
            touched += 1
            if not apply:
                continue
            if profile is not None:
                profile.points = max(0, int(profile.points or 0) - int(c.points or 0))
                profile.verified_at = since
                profile.backfilled = True
            if opening is not None:
                card = db.query(QuestCard).filter(QuestCard.user_id == c.user_id,
                                                  QuestCard.card_id == opening.card_id).first()
                if card is not None:
                    card.count = int(card.count or 0) - 1
                    if card.count <= 0:
                        db.delete(card)
                db.delete(opening)
            db.delete(c)
            db.add(AuditLog(actor_id=c.user_id, action="quests_revoke_backfill",
                            resource_type="user", resource_id=str(c.user_id),
                            detail=json.dumps({"quest_id": c.quest_id, "points": c.points,
                                                    "since": since.isoformat(timespec="seconds")})))
        # Profiles without revoked awards also get a cursor no earlier than the season start.
        if apply:
            for p in db.query(QuestProfile).all():
                if p.verified_at is None or p.verified_at < since:
                    p.verified_at = since
            db.commit()
        print(f"{'APPLIED' if apply else 'DRY RUN'}: {touched} backfill award(s)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
