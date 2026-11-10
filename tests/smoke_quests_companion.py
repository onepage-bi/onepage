"""ADR-17b "Companion": the creature behind the full 8-card collection.

What must hold:
  * 7/8 → no `companion` in state or badge; PUT /api/quests/companion → 403;
  * 8/8 → state carries {seed, since, enabled, autumn}; the page render (the
    mutating path) stamps `companion_since` exactly once and audits it;
    GET /api/quests/state never writes;
  * the badge carries the same block (one COUNT more, only for 8/8);
  * PUT toggles enabled/autumn, audited, both idempotent;
  * the admin overlay `companion_enabled=false` hides everything, and the
    switches refuse (403) while it is off;
  * admin stats count the people with the creature.
"""
import json
import os
import sys
import tempfile
from datetime import datetime

TMP = tempfile.mkdtemp(prefix="op-companion-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_QUESTS_ENABLED": "true",
    "OP_SAML_SP_BASE_URL": "http://testserver",
})
sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AuditLog, QuestCard, QuestProfile, User  # noqa: E402
from app.services import quests, quests_config  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def give_cards(uid, n):
    db = SessionLocal()
    db.query(QuestCard).filter(QuestCard.user_id == uid).delete()
    now = datetime.utcnow()
    # The companion is earned by the SEASON 1 set, so hand out that season's cards,
    # not the active season's: otherwise eight frogs would mean "fox set incomplete".
    for cid in quests.season_card_ids(quests.COMPANION_SEASON)[:n]:
        db.add(QuestCard(user_id=uid, card_id=cid, count=1, first_at=now, last_at=now))
    db.commit(); db.close()


with TestClient(app) as c:
    c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
    db = SessionLocal()
    uid = db.query(User.id).filter(User.person_id == "admin1").scalar()
    db.close()
    c.get("/quests")  # creates the profile

    # ── 7/8: nothing ──────────────────────────────────────────────────
    give_cards(uid, 7)
    st = c.get("/api/quests/state").json()
    check("7/8: no companion in state", st.get("companion") is None, st.get("companion"))
    b = c.get("/api/quests/badge").json()
    check("7/8: no companion in badge", "companion" not in b, b)
    r = c.put("/api/quests/companion", json={"enabled": False})
    check("7/8: switches refused (403)", r.status_code == 403, r.status_code)

    # ── 8/8 ───────────────────────────────────────────────────────────
    give_cards(uid, 8)
    st = c.get("/api/quests/state").json()
    comp = st.get("companion") or {}
    check("8/8: state carries companion", bool(comp), st.get("companion"))
    check("8/8: seed is 8 hex chars", len(comp.get("seed", "")) == 8, comp.get("seed"))
    check("8/8: GET state does not stamp since", comp.get("since") is None, comp)
    check("8/8: enabled by default", comp.get("enabled") is True, comp)
    check("8/8: autumn off by default", comp.get("autumn") is False, comp)

    r = c.get("/quests")
    check("page render 200", r.status_code == 200, r.status_code)
    db = SessionLocal()
    prof = db.get(QuestProfile, uid)
    since1 = prof.companion_since
    reveals = db.query(AuditLog).filter(AuditLog.action == "quests_companion_reveal").count()
    db.close()
    check("page render stamps companion_since", since1 is not None)
    check("reveal audited once", reveals == 1, reveals)
    c.get("/quests")
    db = SessionLocal()
    prof = db.get(QuestProfile, uid)
    check("second render keeps the same since", prof.companion_since == since1)
    check("still one reveal audit", db.query(AuditLog).filter(AuditLog.action == "quests_companion_reveal").count() == 1)
    db.close()

    b = c.get("/api/quests/badge").json()
    check("badge carries companion", (b.get("companion") or {}).get("seed") == comp["seed"], b)
    check("badge companion carries since", bool((b.get("companion") or {}).get("since")), b)

    # ── switches ──────────────────────────────────────────────────────
    r = c.put("/api/quests/companion", json={"enabled": False, "autumn": True})
    check("PUT switches 200", r.status_code == 200, r.text[:200])
    j = r.json().get("companion") or {}
    check("PUT reflects enabled=false", j.get("enabled") is False, j)
    check("PUT reflects autumn=true", j.get("autumn") is True, j)
    st = c.get("/api/quests/state").json()
    check("state reflects the switches",
          st["companion"]["enabled"] is False and st["companion"]["autumn"] is True, st["companion"])
    db = SessionLocal()
    rows = db.query(AuditLog).filter(AuditLog.action == "quests_companion_set").all()
    check("switch audited with field names", len(rows) == 1 and set(json.loads(rows[0].detail)) == {"enabled", "autumn"},
          [r_.detail for r_ in rows])
    db.close()
    r = c.put("/api/quests/companion", json={})
    check("empty PUT is a no-op 200", r.status_code == 200, r.status_code)

    # ── admin overlay off ─────────────────────────────────────────────
    r = c.put("/api/admin/quests/config", json={"companion_enabled": False})
    check("admin can switch the creature off", r.status_code == 200, r.text[:200])
    check("config reports companion_enabled false", r.json().get("companion_enabled") is False, r.json())
    st = c.get("/api/quests/state").json()
    check("overlay off: no companion in state", st.get("companion") is None, st.get("companion"))
    check("overlay off: no companion in badge", "companion" not in c.get("/api/quests/badge").json())
    r = c.put("/api/quests/companion", json={"enabled": True})
    check("overlay off: switches refused (403)", r.status_code == 403, r.status_code)
    r = c.put("/api/admin/quests/config", json={"reset": ["quests_companion_enabled"]})
    check("overlay reset restores", r.status_code == 200 and r.json().get("companion_enabled") is True, r.text[:200])

    # ── stats ─────────────────────────────────────────────────────────
    r = c.get("/api/admin/quests/stats")
    check("stats count companions", r.status_code == 200 and r.json()["summary"].get("companions") == 1,
          r.status_code if r.status_code != 200 else r.json()["summary"].get("companions"))

print()
print("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}")
sys.exit(1 if FAILURES else 0)
