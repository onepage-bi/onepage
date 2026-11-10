"""Smoke test for deploy/backup.sh + deploy/restore.sh.

Builds a small synthetic SQLite DB (WAL mode, matching app/database.py) and a
synthetic uploads/ tree, runs backup.sh several times to exercise retention,
then restore.sh on the latest archive, and compares the restored content
byte-for-byte against the source. Also checks the failure path (missing DB
must exit non-zero) and the --dest2 second-copy option.

No real data, no people's names — synthetic rows only, per the repo's AI data
policy.
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_SH = os.path.join(REPO_ROOT, "deploy", "backup.sh")
RESTORE_SH = os.path.join(REPO_ROOT, "deploy", "restore.sh")


def make_source_db(db_path, rows):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, label TEXT)")
    con.executemany("INSERT INTO widgets (id, label) VALUES (?, ?)", rows)
    con.commit()
    con.close()


def dir_hash(path):
    """Hash of every file's relative path + content, for tree comparison."""
    h = hashlib.sha256()
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, path)
            h.update(rel.encode())
            with open(fp, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    work = tempfile.mkdtemp(prefix="op-backup-smoke-")
    try:
        src = os.path.join(work, "source")
        db_path = os.path.join(src, "onepage.db")
        uploads_dir = os.path.join(src, "uploads")
        os.makedirs(os.path.join(uploads_dir, "42"), exist_ok=True)
        os.makedirs(src, exist_ok=True)

        rows = [(1, "alpha"), (2, "beta"), (3, "gamma")]
        make_source_db(db_path, rows)
        with open(os.path.join(uploads_dir, "42", "report.html"), "w") as f:
            f.write("<html>synthetic dashboard render</html>")
        with open(os.path.join(uploads_dir, "readme.txt"), "w") as f:
            f.write("synthetic upload tree\n")

        # Admin runtime overlays that live NEXT TO the db (same dir as
        # onepage.db — see app/services/runtime_config.py / quests_config.py)
        # must survive a backup+restore cycle, not just onepage.db/uploads.
        llm_runtime = os.path.join(src, "llm-runtime.json")
        with open(llm_runtime, "w") as f:
            f.write('{"provider": "synthetic", "api_key": "synthetic-secret"}')
        os.chmod(llm_runtime, 0o600)
        quests_runtime = os.path.join(src, "quests-runtime.json")
        with open(quests_runtime, "w") as f:
            f.write('{"weekend_rule": true}')

        # perflog/ next to the db must NOT be pulled into the archive (it is
        # telemetry, not data worth restoring, and can be sizeable).
        perflog_dir = os.path.join(src, "perflog")
        os.makedirs(perflog_dir, exist_ok=True)
        with open(os.path.join(perflog_dir, "2026-09-06.jsonl"), "w") as f:
            f.write('{"synthetic": true}\n')

        # A stray WAL/SHM file next to the live db must not end up in the
        # archive either (it belongs to the live file, not the backup copy).
        with open(db_path + "-wal", "wb") as f:
            f.write(b"\x00" * 16)

        # ---- basic backup ----
        dest = os.path.join(work, "backups")
        dest2 = os.path.join(work, "backups2")
        r = run([BACKUP_SH, "--dest", dest, "--dest2", dest2,
                 "--db", db_path, "--uploads", uploads_dir, "--retain", "3"])
        check("backup.sh (first run) -> exit 0", r.returncode == 0, r.stdout + r.stderr)

        archives = sorted(f for f in os.listdir(dest) if f.startswith("onepage-backup-"))
        check("exactly one archive created", len(archives) == 1, str(archives))
        check("--dest2 received a copy of the archive",
              os.path.isfile(os.path.join(dest2, archives[0])) if archives else False)

        # ---- permissions: archive and its directories are private ----
        if archives:
            archive_mode = oct(os.stat(os.path.join(dest, archives[0])).st_mode & 0o777)
            check("archive created with mode 0600", archive_mode == "0o600", archive_mode)
        dest_mode = oct(os.stat(dest).st_mode & 0o777)
        check("--dest created with mode 0700", dest_mode == "0o700", dest_mode)
        dest2_mode = oct(os.stat(dest2).st_mode & 0o777)
        check("--dest2 created with mode 0700", dest2_mode == "0o700", dest2_mode)

        # ---- restore ----
        restore_dir = os.path.join(work, "restored")
        archive_path = os.path.join(dest, archives[0]) if archives else ""
        r = run([RESTORE_SH, "--archive", archive_path, "--dest", restore_dir])
        check("restore.sh -> exit 0", r.returncode == 0, r.stdout + r.stderr)
        check("restore.sh output contains integrity_check: ok",
              "integrity_check: ok" in r.stdout, r.stdout)

        restored_db = os.path.join(restore_dir, "onepage.db")
        check("restored DB exists", os.path.isfile(restored_db))
        if os.path.isfile(restored_db):
            con = sqlite3.connect(restored_db)
            got = con.execute("SELECT id, label FROM widgets ORDER BY id").fetchall()
            con.close()
            check("DB content matches after restore", got == rows, str(got))

        restored_uploads = os.path.join(restore_dir, "uploads")
        check("uploads/ restored",
              dir_hash(restored_uploads) == dir_hash(uploads_dir) if os.path.isdir(restored_uploads) else False)

        restored_llm = os.path.join(restore_dir, "llm-runtime.json")
        check("llm-runtime.json restored next to the DB",
              os.path.isfile(restored_llm) and open(restored_llm).read() == open(llm_runtime).read())
        restored_quests = os.path.join(restore_dir, "quests-runtime.json")
        check("quests-runtime.json restored next to the DB",
              os.path.isfile(restored_quests) and open(restored_quests).read() == open(quests_runtime).read())
        check("perflog/ NOT in the archive (telemetry, not data)",
              not os.path.exists(os.path.join(restore_dir, "perflog")))
        # PRAGMA integrity_check on a WAL-mode db may itself create a fresh
        # (empty) -wal file — that's fine. What must NOT survive is the
        # STRAY sentinel bytes we wrote next to the live db above.
        restored_wal = os.path.join(restore_dir, "onepage.db-wal")
        stray_wal_leaked = os.path.isfile(restored_wal) and open(restored_wal, "rb").read() == b"\x00" * 16
        check("live DB WAL NOT in the archive (consistent only with the snapshot copy)",
              not stray_wal_leaked)

        # ---- retention: run backup 5 more times with --retain 3 ----
        for _ in range(5):
            r = run([BACKUP_SH, "--dest", dest, "--db", db_path,
                     "--uploads", uploads_dir, "--retain", "3"])
            check("backup.sh (second run) -> exit 0", r.returncode == 0, r.stdout + r.stderr)
        archives_after = sorted(f for f in os.listdir(dest) if f.startswith("onepage-backup-"))
        check("retention keeps at most 3 archives in --dest",
              len(archives_after) <= 3, str(archives_after))
        # dest2 only ever got one copy (no --dest2 on the retention runs) —
        # its own retention still must not touch that single file.
        archives_dest2 = sorted(f for f in os.listdir(dest2) if f.startswith("onepage-backup-"))
        check("--dest2 untouched by retention from other runs", len(archives_dest2) == 1, str(archives_dest2))

        # ---- failure path: missing DB ----
        missing_db = os.path.join(work, "no-such.db")
        bad_dest = os.path.join(work, "bad-backups")
        r = run([BACKUP_SH, "--dest", bad_dest, "--db", missing_db, "--uploads", uploads_dir])
        check("backup.sh with a missing DB -> exit != 0", r.returncode != 0, r.stdout + r.stderr)

        # ---- failure path: missing archive ----
        r = run([RESTORE_SH, "--archive", os.path.join(work, "no-such.tar.gz"),
                 "--dest", os.path.join(work, "bad-restore")])
        check("restore.sh with a missing archive -> exit != 0", r.returncode != 0, r.stdout + r.stderr)

        # ---- failure path: corrupted archive fails integrity_check ----
        corrupt_dir = os.path.join(work, "corrupt-src")
        os.makedirs(corrupt_dir, exist_ok=True)
        with open(os.path.join(corrupt_dir, "onepage.db"), "wb") as f:
            f.write(b"not a real sqlite database, just garbage bytes" * 20)
        os.makedirs(os.path.join(corrupt_dir, "uploads"), exist_ok=True)
        corrupt_archive = os.path.join(work, "corrupt.tar.gz")
        r = run(["tar", "-C", corrupt_dir, "-czf", corrupt_archive, "."])
        check("prepare a corrupted archive for the test", r.returncode == 0, r.stderr)
        r = run([RESTORE_SH, "--archive", corrupt_archive, "--dest", os.path.join(work, "corrupt-restore")])
        check("restore.sh rejects a DB failing integrity_check -> exit != 0",
              r.returncode != 0, r.stdout + r.stderr)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
