#!/usr/bin/env python3
"""Why a person with a full collection did not get the companion.

    python3 tools/quests_companion_doctor.py <person_id>

Read-only: the script changes nothing in the database or on disk. Run it on
production inside the container:

    sudo docker exec -i onepage-onepage-1 python3 - <person_id> \\
        < tools/quests_companion_doctor.py

Checks, in order, everything the appearance depends on, and at each step says
not just "bad" but exactly what to do. The order is deliberate: if the flag is
off, nothing else matters.

  1. the account and the quests profile;
  2. the collection: eight DIFFERENT cards (special course cards do not count);
  3. the `quests_companion_enabled` flag — and where it came from, env or the
     admin overlay `data/quests-runtime.json`;
  4. the `companion_since` stamp — ONLY a visit to /quests or opening a chest
     sets it; a plain GET does not write it. The most common reason for "it
     did not appear": the person collected the eighth card and never came
     back to /quests;
  5. the person's own switch (`companion_off`);
  6. exactly what `GET /api/quests/badge` returns for them — the same thing
     the browser will see;
  7. sprite files on disk: the manifest and every clip in it.

At the end — client-side conditions invisible from the database: the response
cache in the browser, the single allowed page and the appearance scheduler.
"""
import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("give a person_id: python3 quests_companion_doctor.py 10000001")
        return 2
    who = sys.argv[1].strip()

    sys.path.insert(0, "/app")
    sys.path.insert(0, os.getcwd())
    from app.config import settings                            # noqa: E402
    from app.database import SessionLocal                      # noqa: E402
    from app.models import (AuditLog, QuestCard, QuestProfile,  # noqa: E402
                            User)
    from app.services import quests, quests_config             # noqa: E402

    ok = "  [ok]   "
    bad = "  [STOP] "
    note = "         "
    print("=" * 72)
    print("Companion: diagnostics for", who)
    print("=" * 72)

    quests_config.ensure_fresh()
    db = SessionLocal()
    try:
        # ── 1. person ─────────────────────────────────────────────────────
        user = db.query(User).filter(User.person_id == who).one_or_none()
        if user is None:
            print(bad + "no such person_id on the platform.")
            print(note + "Check the spelling: SSO sends a UPN, the database holds the numeric id.")
            return 1
        print(ok + "person found: id=%s, %s, active=%s"
              % (user.id, user.display_name or "—", bool(user.is_active)))

        profile = db.get(QuestProfile, user.id)
        if profile is None:
            print(bad + "no quests profile — the person has never opened /quests.")
            print(note + "Ask them to open /quests: the profile is created on the first visit.")
            return 1
        print(ok + "quests profile exists (points %s, streak %s)"
              % (profile.points, profile.streak))

        # ── 2. collection ─────────────────────────────────────────────────
        rows = (db.query(QuestCard.card_id, QuestCard.count)
                .filter(QuestCard.user_id == user.id, QuestCard.count > 0).all())
        # The companion is for the full SEASON 1 set, so the canon comes from
        # it, not from the active season (otherwise the doctor would count toads).
        canon = quests.season_card_ids(quests.COMPANION_SEASON)
        owned = sorted({r[0] for r in rows if r[0] in canon})
        extra = sorted({r[0] for r in rows if r[0] not in canon})
        missing = [c for c in canon if c not in owned]
        if len(owned) < len(canon):
            print(bad + "collection incomplete: %d of %d." % (len(owned), len(canon)))
            print(note + "missing: " + ", ".join(missing))
            if extra:
                print(note + "outside the set (do NOT count): " + ", ".join(extra))
            print(note + "The companion requires exactly eight DIFFERENT cards of the set.")
            return 1
        print(ok + "collection complete: %d of %d" % (len(owned), len(canon)))
        if extra:
            print(note + "plus special course cards: " + ", ".join(extra))

        # ── 3. flag ───────────────────────────────────────────────────────
        overlay_path = os.path.join(os.path.dirname(settings.db_path), "quests-runtime.json") \
            if getattr(settings, "db_path", None) else "data/quests-runtime.json"
        overlay = {}
        try:
            with open(overlay_path, encoding="utf-8") as f:
                overlay = json.load(f)
        except Exception:
            pass
        if not settings.quests_companion_enabled:
            print(bad + "the companion is disabled by the quests_companion_enabled flag.")
            print(note + "overlay %s: %s" % (overlay_path, json.dumps(overlay, ensure_ascii=False)))
            print(note + "Turn it on at /admin/quests, no restart needed.")
            return 1
        src = "admin overlay" if "quests_companion_enabled" in overlay else "environment variable"
        print(ok + "flag enabled (%s)" % src)

        # ── 4. stamp ─────────────────────────────────────────────────────
        if profile.companion_since is None:
            print(bad + "no companion_since stamp — the companion is not \"revealed\" yet.")
            print(note + "ONLY a visit to /quests or opening a chest sets it;")
            print(note + "ordinary page views write nothing. Ask the person")
            print(note + "to open /quests — they will see the intro and the den in \"Collection\" there.")
            return 1
        print(ok + "stamp present: companion_since = %s" % profile.companion_since)

        # ── 5. the person's own switch ────────────────────────────────────
        if profile.companion_off:
            print(bad + "the person turned the companion off themselves (companion_off).")
            print(note + "To bring it back: the switch in the \"Collection\" block on /quests.")
            return 1
        print(ok + "the person has not turned the companion off")

        # ── 6. what the browser sees ───────────────────────────────────────
        info = quests.companion_info(profile, len(owned))
        print(ok + "/api/quests/badge response for them: companion=%s"
              % json.dumps(info, ensure_ascii=False))

        reveals = (db.query(AuditLog)
                   .filter(AuditLog.actor_id == user.id,
                           AuditLog.action == "quests_companion_reveal").count())
        print(note + "quests_companion_reveal rows in the audit log: %d" % reveals)
    finally:
        db.close()

    # ── 7. sprite files ──────────────────────────────────────────────────
    base = "/app/static/img/quests/companion"
    if not os.path.isdir(base):
        base = os.path.join(os.getcwd(), "static", "img", "quests", "companion")
    manifest = os.path.join(base, "sheets", "sheets.json")
    if not os.path.exists(manifest):
        print(bad + "sprite manifest missing: " + manifest)
        print(note + "Without it the module silently falls back to spare poses or draws nothing.")
        print(note + "The files ship with the patch; check that the patch was applied in full.")
        return 1
    try:
        with open(manifest, encoding="utf-8") as f:
            sheets = json.load(f)
    except Exception as exc:
        print(bad + "manifest unreadable: %s" % exc)
        return 1
    # The manifest is a flat dict "clip name -> {file, frames, fps, ...}".
    # It has no "clips" key: the first version of this check looked for one and
    # silently reported "0 clips" on a perfectly intact set.
    clips = sheets.get("clips", sheets) if isinstance(sheets, dict) else sheets
    names = list(clips.keys()) if isinstance(clips, dict) else [c.get("name") for c in (clips or [])]
    # Presence is not enough: an interrupted docker cp leaves the file in place
    # but shorter than it should be — the module then silently draws no frame
    # while the check shows green (prod, 16.09: the owner asked "did the fox
    # actually arrive", and the answer had to be assembled by hand). Every clip
    # in the manifest carries `bytes` — compare against it.
    lost, short = [], []
    for entry in (clips.values() if isinstance(clips, dict) else (clips or [])):
        f = entry.get("file") if isinstance(entry, dict) else None
        if not f:
            continue
        path = os.path.join(base, "sheets", f)
        if not os.path.exists(path):
            lost.append(f)
            continue
        want = entry.get("bytes")
        if isinstance(want, int) and want > 0:
            got = os.path.getsize(path)
            if got != want:
                short.append("%s: %d bytes instead of %d" % (f, got, want))
    if lost:
        print(bad + "the manifest lists clips that are not on disk: " + ", ".join(sorted(set(lost))))
        return 1
    if short:
        print(bad + "sprites on disk have the wrong size — the files did not arrive in full:")
        for line in sorted(set(short)):
            print(note + line)
        print(note + "Re-ship the assets with the full patch; a partially copied")
        print(note + "sprite will not be drawn, while on the server everything looks intact.")
        return 1
    if not names:
        print(bad + "the manifest has no clips — the sprite set is empty.")
        return 1
    print(ok + "sprites in place: %d clips (%s)" % (len(names), ", ".join(map(str, names))))

    # ── what is left on the browser side ──────────────────────────────────
    print("-" * 72)
    print("The server side is fine. What remains is the person's browser:")
    print("""
  1. PAGE. The companion appears ONLY on the report list — the home page "/".
     Never on a report page, in Quests or in the admin area.

  2. CACHE. The browser remembers the companion response in localStorage under
     the key op-quests-companion-<person_id>  (person_id, not the internal id).
     A NEGATIVE badge response is cached separately — for 45 minutes. So a
     person who collected the eighth card on /quests sees nothing on the
     report list until that window ends: the browser simply does not ask again.
     Fixed immediately — open "/", F12 -> Console and run

         localStorage.removeItem('op-quests-companion-' + window.OP_USER.person_id);
         localStorage.removeItem('op-quests-badge-' + window.OP_USER.person_id);
         location.reload();

     Since the 16.09 build this is not needed: the Quests page itself writes
     the response into the same cache, and the fox appears on the report list
     right after the companion is revealed.

  3. WAITING. On the first day after the reveal the companion appears almost
     immediately (2 seconds). After that — at most twice a day, no earlier than
     40 seconds after page load and with a probability of about 45 % per
     visit: this is intentional, so it does not become an eyesore. Leave the
     report list tab open for a couple of minutes.

  4. MOTION. With the system setting "reduce motion"
     (prefers-reduced-motion) scenes are not scheduled at all.

  5. TAB. One scene per tab: if it has already appeared in this tab, it will
     not show again in the same tab — reload the page.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
