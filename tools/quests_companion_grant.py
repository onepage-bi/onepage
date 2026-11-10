#!/usr/bin/env python3
"""Top up a person's missing set cards — to test the companion.

    python3 tools/quests_companion_grant.py <person_id>            # plan only
    python3 tools/quests_companion_grant.py <person_id> --apply    # write
    python3 tools/quests_companion_grant.py <person_id> --undo     # remove ours

This is a testing tool, not a gift: it touches ONLY the collection table and
only the cards the person does not have. Points, streak, rating, quiz and the
daily chest are unchanged — so the person's place in the rating does not move.

Every topped-up card is marked in the audit log (`quests_companion_grant`), and
`--undo` removes exactly those, leaving honestly dropped ones alone. The script does
not set the `companion_since` mark: visiting /quests sets it — and that path is exactly
what needs testing.
"""
import os
import sys
from datetime import datetime, timezone


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print("specify person_id")
        return 2
    who = args[0].strip()

    sys.path.insert(0, "/app")
    sys.path.insert(0, os.getcwd())
    from app.database import SessionLocal                       # noqa: E402
    from app.models import AuditLog, QuestCard, User            # noqa: E402
    from app.services import quests                             # noqa: E402

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.person_id == who).one_or_none()
        if user is None:
            print("no such person_id:", who)
            return 1
        # Grant the missing SEASON 1 cards: the companion is for that season only.
        canon = quests.season_card_ids(quests.COMPANION_SEASON)
        meta = quests.card_meta()
        name = lambda c: meta.get(c, {}).get("name", c)         # noqa: E731

        granted = {a.resource_id for a in db.query(AuditLog).filter(
            AuditLog.actor_id == user.id,
            AuditLog.action == "quests_companion_grant").all()}

        if "--undo" in flags:
            if not granted:
                print("the script never topped up this person — nothing to remove")
                return 0
            for cid in sorted(granted):
                row = db.query(QuestCard).filter(
                    QuestCard.user_id == user.id, QuestCard.card_id == cid).first()
                if row is not None:
                    db.delete(row)
                    print("  removed:", name(cid))
            db.query(AuditLog).filter(
                AuditLog.actor_id == user.id,
                AuditLog.action == "quests_companion_grant").delete()
            db.commit()
            print("done. The collection is back to what the person collected themselves.")
            return 0

        owned = {r[0] for r in db.query(QuestCard.card_id).filter(
            QuestCard.user_id == user.id, QuestCard.count > 0).all()}
        missing = [c for c in canon if c not in owned]
        print("%s: %d of %d" % (user.display_name or who, len(canon) - len(missing), len(canon)))
        if not missing:
            print("the set is already complete — nothing to top up.")
            return 0
        print("missing:", ", ".join(name(c) for c in missing))
        if "--apply" not in flags:
            print("\nthis is only a plan. To write it, repeat with --apply")
            return 0

        # The database stores UTC without a zone — convert to the same form explicitly.
        stamp = datetime.now(timezone.utc).replace(tzinfo=None)
        for cid in missing:
            db.add(QuestCard(user_id=user.id, card_id=cid, count=1,
                             first_at=stamp, last_at=stamp))
            rec = AuditLog(actor_id=user.id, action="quests_companion_grant",
                           resource_type="quest_card", resource_id=cid)
            rec.set_detail({"reason": "companion check", "by": "tools/quests_companion_grant.py"})
            db.add(rec)
            print("  granted:", name(cid))
        db.commit()
        print("\ndone. Now open /quests — the intro and the burrow are in \"Collection\",")
        print("after that the fox appears on the report list \"/\".")
        print("To undo:  python3 - %s --undo" % who)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
