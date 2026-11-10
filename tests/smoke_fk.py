"""Integrity: `foreign_keys=ON` and orphans (2026-09-06).

For this application's whole life SQLite ran with foreign-key checks off, so
turning them on raises two different questions, and the test closes both.

  * **Forward**: after start-up the check really is on (not "we wrote the
    PRAGMA" but "a broken insert fails"), and no parent deletion — dashboard,
    account, presentation, folder, reference table — trips over its own
    children or leaves them dangling. Checked by rows in the DB, not by the
    response code: a 200 with an orphaned tail is exactly the bug caught here.
  * **Backward**: `tools/fk_orphans.py` finds orphans in a file written without
    the check, repairs what can be repaired, and does NOT touch references to
    `users` — returning 2 so a deploy cannot slip past a person's decision.
    "Repairs" follows the same policy as live deletions, and that matters
    precisely here: a deleted dashboard's NOT NULL tail is removed, while a
    nullable reference is NULLed and the row lives on. Otherwise a run on the
    production DB (docs/DEPLOY.md §2d) would wipe presentation slides and
    recorded AI answers orphaned before this branch.

Plus `identity.merge_duplicates` with the check on: it moves rows between
accounts with raw SQL, and before this branch any IntegrityError meant "the
canonical account already has such a row" and led to a DELETE.

The data is entirely synthetic.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-fk-")
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

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import text                        # noqa: E402
from sqlalchemy.exc import IntegrityError          # noqa: E402

from app.main import app                           # noqa: E402
from app.database import SessionLocal              # noqa: E402
from app.models import (                           # noqa: E402
    AuditLog, Dashboard, DashboardFavorite, DashboardPermission, DashboardVersion,
    DataSource, Employee, Generator, GeneratorVersion, GenerationRun, LlmAccess,
    ExploreAnswer, Presentation, PresentationItem, QualityResult, ReferenceTable, RunReferenceUse,
    RunStage, SourceFile, SourceObservation, User,
    ROLE_ADMINISTRATOR, ROLE_SUPERUSER, ROLE_VISITOR,
)
from app.services import identity                  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def count(db, table, where="1=1"):
    return db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar()


def run_tool(db_path, *args):
    """tools/fk_orphans.py the way an admin runs it on prod — a separate
    process, by file path, sharing no session with the application."""
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "tools", "fk_orphans.py"),
         "--db", db_path, *args],
        cwd=REPO, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": REPO},
    )
    return proc.returncode, proc.stdout + proc.stderr


# ── building the synthetic graph ─────────────────────────────────────────

def make_graph(db, owner, dash_id):
    """A dashboard with the whole tail hanging on it without a relationship:
    run → observation → file → check result, stages, reference-table use,
    favourite, permission, version."""
    d = Dashboard(id=dash_id, title="T", owner_id=owner.id)
    db.add(d)
    db.flush()
    g = Generator(name=f"gen-{dash_id}", created_by=owner.id)
    db.add(g)
    db.flush()
    gv = GeneratorVersion(generator_id=g.id, version_number=1, source_type="bundle",
                          bundle_path="b", manifest_json="{}", checksum_sha256="x",
                          created_by=owner.id)
    db.add(gv)
    db.flush()
    run = GenerationRun(dashboard_id=d.id, generator_version_id=gv.id, engine="browser",
                        status="ok", started_by=owner.id)
    db.add(run)
    db.flush()
    ds = DataSource(kind="folder", key_name=f"src-{dash_id}")
    db.add(ds)
    db.flush()
    obs = SourceObservation(run_id=run.id, data_source_id=ds.id)
    db.add(obs)
    db.flush()
    sfile = SourceFile(observation_id=obs.id, name="f.xlsx")
    db.add(sfile)
    db.flush()
    rt = ReferenceTable(slug=f"ref-{dash_id}", title="R", key_column="k",
                        columns_json="[]", created_by=owner.id)
    db.add(rt)
    db.flush()
    db.add_all([
        QualityResult(source_file_id=sfile.id, observation_id=obs.id,
                      check_id="c", check_type="profile", status="pass"),
        RunStage(run_id=run.id, order=1, name="stage"),
        RunReferenceUse(run_id=run.id, table_id=rt.id, version_number=1),
        DashboardVersion(dashboard_id=d.id, version_number=1, html_path="h"),
        DashboardPermission(dashboard_id=d.id, user_id=owner.id),
        DashboardFavorite(user_id=owner.id, dashboard_id=d.id),
    ])
    db.commit()
    return run.id


TAIL = ["generation_runs", "source_observations", "source_files", "quality_results",
        "run_stages", "run_reference_uses", "dashboard_versions",
        "dashboard_permissions", "dashboard_favorites"]


with TestClient(app) as c:
    db = SessionLocal()
    admin = db.query(User).filter(User.person_id == "admin1").first()
    assert admin is not None and admin.role == ROLE_ADMINISTRATOR

    # ── 1. the check really is on ────────────────────────────────────────
    check("PRAGMA foreign_keys = 1 after start-up",
          db.execute(text("PRAGMA foreign_keys")).scalar() == 1)

    try:
        db.add(DashboardFavorite(user_id=admin.id, dashboard_id="no-such-dashboard"))
        db.commit()
        broke = False
    except IntegrityError as exc:
        db.rollback()
        broke = "FOREIGN KEY" in str(exc.orig or exc).upper()
    check("an insert with a broken FK is rejected", broke)

    # ── 2. deleting a dashboard leaves no tail ───────────────────────────
    pres_keep = Presentation(id="pres-keep", title="P", owner_id=admin.id)
    db.add(pres_keep)
    db.flush()
    run_id = make_graph(db, admin, "dash-1")
    db.add(PresentationItem(id="item-1", presentation_id=pres_keep.id,
                            dashboard_id="dash-1", kind="dashboard", position=0))
    # explore_answers.dashboard_id is declared Integer against a VARCHAR
    # parent — SQLite compares by the parent's affinity, so the reference is
    # live and must be NULLed along with all the others.
    gen1 = db.query(Generator).filter(Generator.name == "gen-dash-1").first()
    db.add(ExploreAnswer(user_id=admin.id, generator_id=gen1.id, dashboard_id="dash-1",
                         question="q", tier="lake", generator_version=1,
                         semantics_sha256="0" * 64, via="server", run_id=run_id))
    db.commit()

    login(c, "admin1")
    r = c.delete("/api/dashboards/dash-1")
    check("DELETE /api/dashboards -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    db.expire_all()
    left = {t: count(db, t) for t in TAIL}
    check("deleting the dashboard left no children", all(v == 0 for v in left.values()), left)
    check("dashboard deleted", count(db, "dashboards", "id = 'dash-1'") == 0)
    check("presentation slide outlived the dashboard, reference NULLed",
          count(db, "presentation_items", "id = 'item-1' AND dashboard_id IS NULL") == 1)
    check("AI answer record outlived the dashboard, reference NULLed",
          count(db, "explore_answers", "question = 'q' AND dashboard_id IS NULL") == 1)
    check("delete audit recorded what it took with it",
          '"cascaded"' in (db.query(AuditLog).filter(AuditLog.action == "delete",
                                                     AuditLog.resource_id == "dash-1")
                           .first().detail or ""))

    # ── 3. deleting an account ───────────────────────────────────────────
    victim = User(person_id="victim", role=ROLE_SUPERUSER)
    db.add(victim)
    db.flush()
    make_graph(db, victim, "dash-2")
    own = Dashboard(id="dash-own", title="Own", owner_id=victim.id)
    db.add(own)
    db.add_all([
        AuditLog(actor_id=victim.id, action="whatever", resource_type="x"),
        LlmAccess(user_id=victim.id, granted_by=admin.id),
    ])
    db.commit()
    victim_id = victim.id

    r = c.delete(f"/api/admin/users/{victim_id}")
    check("DELETE /api/admin/users -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    db.expire_all()
    check("account deleted", count(db, "users", f"id = {victim_id}") == 0)
    check("permissions/favourites/LLM access deleted with the account",
          count(db, "dashboard_permissions", f"user_id = {victim_id}") == 0
          and count(db, "dashboard_favorites", f"user_id = {victim_id}") == 0
          and count(db, "llm_access", f"user_id = {victim_id}") == 0)
    check("the dashboard outlived its owner, owner_id NULLed",
          count(db, "dashboards", "id = 'dash-own' AND owner_id IS NULL") == 1)
    check("audit row outlived its actor, actor_id NULLed",
          count(db, "audit_log", "action = 'whatever' AND actor_id IS NULL") == 1)
    check("the deleted account's runs were not orphaned",
          count(db, "generation_runs", f"started_by = {victim_id}") == 0)

    # ── 4. presentation ──────────────────────────────────────────────────
    r = c.delete("/api/presentations/pres-keep")
    check("DELETE /api/presentations -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    db.expire_all()
    check("slides deleted with the presentation",
          count(db, "presentations", "id = 'pres-keep'") == 0
          and count(db, "presentation_items", "presentation_id = 'pres-keep'") == 0)

    # ── 5. folder and reference table (their own code clears children) ──
    r = c.post("/api/folders", json={"name": "Folder"})
    check("create folder -> 200", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    folder_id = r.json().get("id")
    db.add(Dashboard(id="dash-3", title="In folder", owner_id=admin.id, folder_id=folder_id))
    db.commit()
    r = c.delete(f"/api/folders/{folder_id}")
    check("DELETE /api/folders -> 200 with FK on", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    db.expire_all()
    check("the folder's dashboard moved to root",
          count(db, "dashboards", "id = 'dash-3' AND folder_id IS NULL") == 1)

    ref = ReferenceTable(slug="ref-del", title="R", key_column="k", columns_json="[]",
                         created_by=admin.id, responsible_user_id=admin.id)
    db.add(ref)
    db.commit()
    r = c.delete("/api/reference/ref-del")
    check("DELETE /api/reference -> 204 with FK on", r.status_code == 204,
          f"{r.status_code} {r.text[:200]}")

    # ── 6. merge_duplicates with the check on ────────────────────────────
    db.close()                      # fresh session: rows deleted above must
    db = SessionLocal()             # not linger in the identity map
    admin = db.query(User).filter(User.person_id == "admin1").first()
    # The case DIFFERS on purpose: the directory stores the address in lower
    # case, SSO sends it however it likes, and the merge must fold them into
    # one person (the casefold branch of _employee_by_email). Aligning these
    # two strings would keep the check green while no longer testing its point.
    db.add(Employee(id=1, person_id="10000002", employeeid="1",
                    email="synthetic_person@example.com", lastname_en="Person",
                    firstname_en="Synthetic", is_active=True))
    canon = User(person_id="10000002", role=ROLE_SUPERUSER)
    dup = User(person_id="Synthetic_Person@Example.com", role=ROLE_VISITOR)
    db.add_all([canon, dup])
    db.flush()
    shared = Dashboard(id="dash-merge", title="M", owner_id=canon.id)
    db.add(shared)
    db.flush()
    db.add_all([
        DashboardPermission(dashboard_id=shared.id, user_id=canon.id, can_update_data=True),
        DashboardPermission(dashboard_id=shared.id, user_id=dup.id),   # clash → the duplicate is dropped
        DashboardFavorite(user_id=dup.id, dashboard_id=shared.id),     # moved over
        AuditLog(actor_id=dup.id, action="merged_actor", resource_type="x"),
    ])
    db.commit()
    canon_id, dup_id = canon.id, dup.id

    res = identity.merge_duplicates(db, apply=True)
    check("merge_duplicates does not fail with foreign_keys=ON", res["applied"] and res["count"] >= 1,
          res)
    db.expire_all()
    check("the duplicate's rows moved to the canonical account",
          count(db, "dashboard_favorites", f"user_id = {canon_id}") == 1
          and count(db, "dashboard_favorites", f"user_id = {dup_id}") == 0
          and count(db, "audit_log", f"action = 'merged_actor' AND actor_id = {canon_id}") == 1)
    check("a unique-key clash is dropped, not moved",
          count(db, "dashboard_permissions", f"dashboard_id = 'dash-merge' AND user_id = {dup_id}") == 0)
    check("no orphans on users after the merge",
          count(db, "dashboard_favorites",
                f"user_id NOT IN (SELECT id FROM users)") == 0)

    # ── 7. tools/fk_orphans.py on a DB copy written without the check ────
    db.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    db.commit()
    copy_path = os.path.join(TMP, "copy.db")
    src = sqlite3.connect(os.environ["OP_DB_PATH"])
    dst = sqlite3.connect(copy_path)
    src.backup(dst)
    src.close()
    dst.close()

    seed = sqlite3.connect(copy_path)
    seed.isolation_level = None
    seed.execute("PRAGMA foreign_keys=OFF")   # exactly the state we are leaving
    live_gen = seed.execute("SELECT id FROM generators LIMIT 1").fetchone()[0]
    seed.executescript(f"""
        INSERT INTO dashboard_favorites (user_id, dashboard_id, position, created_at)
             VALUES ({canon_id}, 'ghost-dashboard', 0, CURRENT_TIMESTAMP);
        INSERT INTO generation_runs (id, dashboard_id, generator_version_id, engine, status, started_at)
             VALUES (90001, 'ghost-dashboard', 999999, 'browser', 'ok', CURRENT_TIMESTAMP);
        INSERT INTO source_observations (id, run_id, data_source_id, dir_index,
                                         file_count, total_bytes, truncated, created_at)
             VALUES (90002, 90001, 999999, 0, 0, 0, 0, CURRENT_TIMESTAMP);
        INSERT INTO source_files (id, observation_id, name, kind)
             VALUES (90003, 90002, 'ghost.xlsx', 'listed');
        INSERT INTO audit_log (actor_id, action, resource_type, created_at)
             VALUES (999999, 'ghost_actor', 'x', CURRENT_TIMESTAMP);

        -- Orphans that appeared on prod LEGITIMATELY: before this branch a
        -- dashboard deletion touched neither presentation slides nor recorded
        -- AI answers. The deck needs the slide (api/presentations.py shows it
        -- as access:false), the answer is the question's history. Rows survive.
        INSERT INTO presentations (id, title, owner_id, created_at, updated_at)
             VALUES ('pres-ghost', 'P', {canon_id}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        INSERT INTO presentation_items (id, presentation_id, dashboard_id, kind,
                                        position, created_at)
             VALUES ('item-ghost', 'pres-ghost', 'ghost-dashboard', 'dashboard',
                     0, CURRENT_TIMESTAMP);
        INSERT INTO explore_answers (id, user_id, generator_id, dashboard_id, question,
                                     tier, generator_version, semantics_sha256, via, created_at)
             VALUES (90004, {canon_id}, {live_gen}, 'ghost-dashboard', 'ghost-q',
                     'lake', 1, '{"0" * 64}', 'server', CURRENT_TIMESTAMP);

        -- A dangling folder and generator reference is NOT a reason to lose
        -- the dashboard: it moves to root and stays unbound from a generator.
        INSERT INTO dashboards (id, title, dash_type, owner_id, is_published, is_public,
                                current_version, slot_count, folder_id, generator_id,
                                created_at, updated_at)
             VALUES ('dash-ghost-refs', 'Ghost refs', 'static', {canon_id}, 0, 0,
                     1, 0, 999999, 999999, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
    """)
    seed.close()

    code, out = run_tool(copy_path)
    check("report: orphans found", "dashboard_favorites.dashboard_id" in out
          and "generation_runs.dashboard_id" in out, out[-800:])
    check("report: nullable references are visible too",
          "presentation_items.dashboard_id" in out and "explore_answers.dashboard_id" in out
          and "dashboards.folder_id" in out and "dashboards.generator_id" in out, out[-800:])
    check("report: values never reach the output",
          "ghost-dashboard" not in out and "ghost.xlsx" not in out, out[-800:])
    check("report: a users reference is flagged and gives exit 2", code == 2 and "audit_log.actor_id" in out,
          f"code={code}\n{out[-800:]}")

    code, out = run_tool(copy_path, "--fix")
    check("--fix: exit 2 (a protected users reference remains)", code == 2, f"code={code}\n{out[-800:]}")
    chk = sqlite3.connect(copy_path)
    check("--fix: dashboard orphans deleted",
          chk.execute("SELECT COUNT(*) FROM dashboard_favorites "
                      "WHERE dashboard_id = 'ghost-dashboard'").fetchone()[0] == 0
          and chk.execute("SELECT COUNT(*) FROM generation_runs WHERE id = 90001").fetchone()[0] == 0)
    check("--fix: children orphaned in turn are deleted too (several passes)",
          chk.execute("SELECT COUNT(*) FROM source_observations WHERE id = 90002").fetchone()[0] == 0
          and chk.execute("SELECT COUNT(*) FROM source_files WHERE id = 90003").fetchone()[0] == 0)
    check("--fix: audit row with a foreign actor_id untouched",
          chk.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'ghost_actor'").fetchone()[0] == 1)
    check("--fix: orphaned presentation slide kept, reference NULLed",
          chk.execute("SELECT COUNT(*) FROM presentation_items "
                      "WHERE id = 'item-ghost' AND dashboard_id IS NULL").fetchone()[0] == 1)
    check("--fix: orphaned AI answer kept, reference NULLed",
          chk.execute("SELECT COUNT(*) FROM explore_answers "
                      "WHERE id = 90004 AND dashboard_id IS NULL").fetchone()[0] == 1)
    check("--fix: dashboard with a dangling folder/generator intact, references NULLed",
          chk.execute("SELECT COUNT(*) FROM dashboards WHERE id = 'dash-ghost-refs' "
                      "AND folder_id IS NULL AND generator_id IS NULL").fetchone()[0] == 1)
    chk.close()

    # a person dealt with the users reference — the DB is clean, exit 0
    fixup = sqlite3.connect(copy_path)
    fixup.isolation_level = None
    fixup.execute("UPDATE audit_log SET actor_id = NULL WHERE action = 'ghost_actor'")
    fixup.close()
    code, out = run_tool(copy_path)
    check("clean DB -> exit 0", code == 0 and "No orphans." in out, f"code={code}\n{out[-500:]}")

    # the live DB after all the API deletions is clean too
    code, out = run_tool(os.environ["OP_DB_PATH"])
    check("live DB after all deletions: no orphans", code == 0, f"code={code}\n{out[-1500:]}")

    db.close()

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL PASSED")
