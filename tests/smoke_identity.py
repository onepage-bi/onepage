"""One person = one account (app/services/identity.py, 2026-08-28).

Prod 2026-08-28: admins added colleagues by numeric person_id (from the
employees directory); every SSO login then arrived as a UPN and made a
second, rightless account. This pins: UPN resolution, login landing on the
canonical account, and the merge of the duplicates the old path created.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-identity-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (AuditLog, Dashboard, DashboardPermission, Employee,  # noqa: E402
                        User, ROLE_SUPERUSER, ROLE_VISITOR)
from app.services import identity  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (("  -- " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        FAILURES.append(label)


def main():
    with TestClient(app) as c:
        c.get("/health")  # schema + migrations
        db = SessionLocal()
        db.add_all([
            Employee(id=1, person_id="10000002", employeeid="1", email="Viktor_Sidorov@example.com",
                     lastname_ru="Sidorov", firstname_ru="Viktor", is_active=True),
            Employee(id=2, person_id="10000001", employeeid="2", email="x@example.com",
                     lastname_ru="Egorova", firstname_ru="Olga", is_active=True),
            Employee(id=3, person_id="10000003", employeeid="3", email="polina_petrova@example.com",
                     lastname_ru="Petrova", firstname_ru="Polina", is_active=True),
        ])
        canon = User(person_id="10000002", display_name="Sidorov Viktor", role=ROLE_SUPERUSER)
        dup = User(person_id="viktor_sidorov@example.com", role=ROLE_VISITOR)      # old login path
        num_dup = User(person_id="10000001@example.com", role=ROLE_VISITOR)            # number@domain, no canonical yet
        stranger = User(person_id="guest@partner.example", role=ROLE_VISITOR)           # not in the directory
        twin_a = User(person_id="10000003@example.com", role=ROLE_VISITOR)             # prod: two UPNs, one number,
        twin_b = User(person_id="Polina_Petrova@example.com", role=ROLE_SUPERUSER)     # no numeric account yet
        db.add_all([canon, dup, num_dup, stranger, twin_a, twin_b])
        db.flush()
        d1 = Dashboard(id="d1", title="A", owner_id=canon.id)
        d2 = Dashboard(id="d2", title="B", owner_id=dup.id)
        db.add_all([d1, d2])
        db.flush()
        db.add_all([
            DashboardPermission(dashboard_id="d1", user_id=canon.id, can_update_data=True),
            DashboardPermission(dashboard_id="d1", user_id=dup.id, can_update_data=False),  # clash → dropped
            DashboardPermission(dashboard_id="d2", user_id=dup.id, can_update_data=True),   # moved
            AuditLog(actor_id=dup.id, action="explore_intent", resource_type="explore"),
        ])
        db.commit()
        from app.auth.session import session_store
        session_store.create(dup.id, dup.person_id, dup.role)   # the old account's live session

        # ── resolve ──────────────────────────────────────────────────────
        check("resolve: number@domain → number", identity.resolve(db, "10000001@example.com") == ("10000001", "10000001@example.com"))
        check("resolve: name@domain → employee person_id, case-insensitive",
              identity.resolve(db, "viktor_sidorov@example.com") == ("10000002", "viktor_sidorov@example.com"))
        check("resolve: numeric stays numeric", identity.resolve(db, "10000002") == ("10000002", None))
        check("resolve: unknown upn stays itself", identity.resolve(db, "guest@partner.example") == ("guest@partner.example", "guest@partner.example"))
        check("resolve: merged: marker never resolves", identity.resolve(db, "merged:x@y") == ("merged:x@y", None))
        db.add_all([Employee(id=5, person_id="10000008", employeeid="5", email="Roman_Orlov_External@example.com",
                             lastname_en="Orlov", firstname_en="Roman", is_active=True),
                    Employee(id=6, person_id="90000001", employeeid="6", email="ivan_ivanov_external@example.com", is_active=True),
                    Employee(id=7, person_id="90000002", employeeid="7", email="ivan_ivanov_external_External@example.com", is_active=True)])
        db.flush()
        check("resolve: the directory's _External suffix is ignored when it leaves ONE match",
              identity.resolve(db, "Roman_Orlov@example.com") == ("10000008", "Roman_Orlov@example.com"),
              identity.resolve(db, "Roman_Orlov@example.com"))
        check("resolve: … but never guesses between two candidates",
              identity.resolve(db, "Ivan_Ivanov@example.com")[0] == "Ivan_Ivanov@example.com")

        # ── login lands on the canonical account ─────────────────────────
        u = identity.find_or_create(db, "viktor_sidorov@example.com")
        check("login: UPN of an existing numeric account → that account, rights intact",
              u.id == canon.id and u.role == ROLE_SUPERUSER, (u.id, u.role))
        check("login: … and the UPN is remembered on it", u.upn == "viktor_sidorov@example.com", u.upn)
        db.add(Employee(id=4, person_id="10000007", employeeid="4", email="artem_smirnov@example.com",
                        lastname_ru="Smirnov", firstname_ru="Artem", is_active=True))
        db.flush()
        u = identity.find_or_create(db, "Artem_Smirnov@example.com")
        check("login: a first login with a directory row creates the NUMERIC account, named",
              u.person_id == "10000007" and u.display_name == "Smirnov Artem" and u.upn == "Artem_Smirnov@example.com",
              (u.person_id, u.display_name, u.upn))
        u2 = identity.find_or_create(db, "10000007")
        check("login: the same person by number is the same account", u2.id == u.id)
        check("login: before the merge, an old UPN account (no numeric one) is still found — rights intact",
              identity.find_or_create(db, "polina_petrova@example.com").id == twin_b.id)
        check("login: an outsider keeps the UPN as id", identity.find_or_create(db, "guest@partner.example").id == stranger.id)
        check("login: an unknown outsider is created as visitor under the UPN",
              identity.find_or_create(db, "new@partner.example").person_id == "new@partner.example")
        db.commit()

        # ── /dev-login goes through the same door ────────────────────────
        r = c.post("/dev-login", data={"person_id": "viktor_sidorov@example.com"}, follow_redirects=False)
        sid = db.execute(text("SELECT user_id FROM sessions ORDER BY created_at DESC LIMIT 1")).fetchone()
        check("dev-login by UPN opens a session on the canonical account",
              r.status_code in (302, 303) and sid and sid[0] == canon.id, (r.status_code, sid))

        # ── merge: dry run touches nothing ───────────────────────────────
        rep = identity.merge_duplicates(db, apply=False)
        items = {i["dup_person_id"]: i for i in rep["items"]}
        check("merge dry-run: finds the UPN duplicate, the number@domain one and the twins, not the outsider",
              set(items) == {"viktor_sidorov@example.com", "10000001@example.com", "10000003@example.com", "Polina_Petrova@example.com"}, sorted(items))
        check("merge dry-run: of two UPNs for one number the first is a rename, the second a merge into it",
              items["10000003@example.com"]["action"] == "rename" and items["Polina_Petrova@example.com"]["action"] == "merge"
              and items["Polina_Petrova@example.com"]["target_id"] == twin_a.id, (items["10000003@example.com"], items["Polina_Petrova@example.com"]))
        check("unresolved: the outsider is listed for the admin, with no directory candidates",
              [x["person_id"] for x in identity.unresolved(db)] == ["guest@partner.example", "new@partner.example"], identity.unresolved(db))
        check("merge dry-run: the UPN one is a merge into the numeric account",
              items["viktor_sidorov@example.com"]["action"] == "merge" and items["viktor_sidorov@example.com"]["target_id"] == canon.id)
        check("merge dry-run: number@domain with no numeric account is a rename",
              items["10000001@example.com"]["action"] == "rename")
        check("merge dry-run: counts what would move", items["viktor_sidorov@example.com"]["moved"].get("dashboard_permissions.user_id") == 2
              and items["viktor_sidorov@example.com"]["moved"].get("dashboards.owner_id") == 1, items["viktor_sidorov@example.com"]["moved"])
        db.expire_all()
        check("merge dry-run: nothing changed", db.get(User, dup.id).is_active and db.get(User, dup.id).person_id == "viktor_sidorov@example.com")

        # ── merge: apply ─────────────────────────────────────────────────
        rep = identity.merge_duplicates(db, apply=True)
        db.expire_all()
        d = db.get(User, dup.id); cn = db.get(User, canon.id); nd = db.get(User, num_dup.id)
        check("merge: the duplicate is deactivated and renamed, never deleted",
              d is not None and not d.is_active and d.person_id == "merged:viktor_sidorov@example.com", (d.is_active, d.person_id))
        perms = {(p.dashboard_id, p.user_id, p.can_update_data) for p in db.query(DashboardPermission).all()}
        check("merge: the d2 permission moved to the canonical account", ("d2", canon.id, True) in perms, perms)
        check("merge: the clashing d1 permission was dropped, the canonical one kept",
              ("d1", canon.id, True) in perms and not any(p[1] == dup.id for p in perms), perms)
        check("merge: dashboard ownership moved", db.get(Dashboard, "d2").owner_id == canon.id)
        check("merge: audit rows follow", db.query(AuditLog).filter(AuditLog.actor_id == canon.id, AuditLog.action == "explore_intent").count() == 1)
        check("merge: the duplicate's sessions are gone",
              db.execute(text("SELECT count(*) FROM sessions WHERE user_id = :u"), {"u": dup.id}).fetchone()[0] == 0)
        check("merge: canonical keeps the higher role and gets the upn", cn.role == ROLE_SUPERUSER and cn.upn == "viktor_sidorov@example.com", (cn.role, cn.upn))
        check("merge: number@domain became the numeric id, named from the directory",
              nd.person_id == "10000001" and nd.upn == "10000001@example.com" and nd.display_name == "Egorova Olga", (nd.person_id, nd.upn, nd.display_name))
        ta = db.get(User, twin_a.id); tb = db.get(User, twin_b.id)
        check("merge: the twins became ONE numeric account — the higher role kept, the other merged",
              ta.person_id == "10000003" and ta.role == ROLE_SUPERUSER and ta.is_active and ta.display_name == "Petrova Polina"
              and not tb.is_active and tb.person_id == "merged:Polina_Petrova@example.com", (ta.person_id, ta.role, tb.person_id))
        check("merge: a second run finds nothing", identity.merge_duplicates(db, apply=False)["count"] == 0)

        # ── the admin's own map for the names the directory spells differently ──
        db.add_all([User(person_id="petr_ivanov@example.com", role=ROLE_SUPERUSER),
                    User(person_id="mikhail_volkov@example.com", role=ROLE_VISITOR),
                    User(person_id="10000004", display_name="Volkov Mikhail", role=ROLE_VISITOR)])
        db.commit()
        rep = identity.merge_manual(db, {"petr_ivanov@example.com": "10000006",
                                         "Mikhail_Volkov@example.com": "10000004",
                                         "nobody@example.com": "1"}, apply=False)
        acts = {i["dup_person_id"]: i["action"] for i in rep["items"]}
        check("manual dry-run: rename when no numeric account, merge when there is one, skip when no such account",
              acts == {"petr_ivanov@example.com": "rename", "mikhail_volkov@example.com": "merge", "nobody@example.com": "skip"}, acts)
        identity.merge_manual(db, {"petr_ivanov@example.com": "10000006", "Mikhail_Volkov@example.com": "10000004"}, apply=True)
        db.expire_all()
        mm = db.query(User).filter(User.person_id == "10000006").first()
        check("manual apply: renamed to the number, UPN kept, role kept",
              mm is not None and mm.upn == "petr_ivanov@example.com" and mm.role == ROLE_SUPERUSER, mm and (mm.upn, mm.role))
        check("manual apply: the next login by that UPN lands on the renamed account",
              identity.find_or_create(db, "Petr_Ivanov@example.com").id == mm.id)
        gk = db.query(User).filter(User.person_id == "10000004").first()
        check("manual apply: merged into the numeric account, which now carries the UPN",
              gk.upn == "mikhail_volkov@example.com" and identity.find_or_create(db, "mikhail_volkov@example.com").id == gk.id)
        check("login after merge: the UPN still lands on the canonical account",
              identity.find_or_create(db, "viktor_sidorov@example.com").id == canon.id)
        check("login after merge: the merged alias itself can never be logged into",
              identity.find_or_create(db, "merged:viktor_sidorov@example.com", create=False) is not None
              and not identity.find_or_create(db, "merged:viktor_sidorov@example.com", create=False).is_active)
        # ── env by UPN keeps working after the merge ─────────────────────
        from app.services import llm_access
        from app.config import settings
        settings.llm_access_admins = "viktor_sidorov@example.com"
        cn = db.get(User, canon.id); cn.role = "administrator"; db.commit()
        check("llm_access: OP_LLM_ACCESS_ADMINS naming the old UPN still matches the merged account",
              llm_access.can_assign(cn) is True)
        settings.llm_access_admins = "10000002"
        check("llm_access: … and naming the number matches too", llm_access.can_assign(cn) is True)
        settings.llm_access_admins = "someone_else@example.com"
        check("llm_access: a different name does not", llm_access.can_assign(cn) is False)
        settings.llm_access_admins = ""
        check("bootstrap: OP_INITIAL_ADMIN by the old UPN finds the merged account instead of creating one",
              identity.find_or_create(db, "viktor_sidorov@example.com", create=False).id == canon.id
              and db.query(User).filter(User.person_id == "viktor_sidorov@example.com").first() is None)
        db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
