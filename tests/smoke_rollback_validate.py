"""Regression for the two review findings on deploy/rollback.sh:

1. The script must validate its argument BEFORE stopping the container.
   A typo'd image tag (or any invalid archive) must exit with an error
   without ever calling `docker compose stop` / `docker stop` — an incident
   rollback must not turn into a second outage just because the operator
   made a typo (restart: unless-stopped does not revive a container that
   was explicitly stopped).

2. A .tar.gz that isn't a full bundle (no onepage-image.tar.gz inside) and
   isn't a clean ./data backup (entries outside data/) must be REJECTED,
   never extracted into the deployment directory (where docker-compose.yml
   and .env with secrets live) — no `tar -xzf` into cwd, no stop, no files
   written, before the content check has run.

3. Integration with deploy/backup.sh (merged alongside): the real
   onepage-backup-*.tar.gz layout is FLAT (onepage.db, uploads/, *.json in
   the archive root, no data/ prefix) and restore.sh takes
   `--archive PATH --dest DIR`. rollback.sh must accept that layout, drive
   restore.sh with the right CLI, and do all extraction + integrity_check in
   a staging dir BEFORE stopping the container — a corrupt backup must not
   cause an outage.

Uses a fake `docker` on PATH that only logs invocations (docker.log) so we
can assert `stop`/`compose stop` was never called.
"""
import os
import stat
import subprocess
import sys
import tarfile
import tempfile

REPO = os.getcwd()
ROLLBACK = os.path.join(REPO, "deploy", "rollback.sh")

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def make_fake_docker(bindir, inspect_ok=False):
    log = os.path.join(bindir, "docker.log")
    script = os.path.join(bindir, "docker")
    body = f"""#!/usr/bin/env bash
echo "docker $*" >> "{log}"
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  {"exit 0" if inspect_ok else "exit 1"}
fi
if [ "$1" = "compose" ]; then
  exit 0
fi
exit 0
"""
    with open(script, "w") as f:
        f.write(body)
    os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return log


def run_rollback(workdir, path, arg, health_retries=None):
    env = dict(os.environ)
    env["PATH"] = path + ":" + env["PATH"]
    if health_retries is not None:
        env["ONEPAGE_HEALTH_RETRIES"] = str(health_retries)
        env["ONEPAGE_PORT"] = "18743"  # unused port -> curl fails fast, no real server needed
    return subprocess.run(
        ["bash", ROLLBACK, arg],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def sqlite_bytes(tmp, name="good.db", corrupt=False):
    """Bytes of a real (or deliberately corrupted) SQLite database."""
    import sqlite3
    path = os.path.join(tmp, name)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('synthetic')")
    con.commit()
    con.close()
    data = open(path, "rb").read()
    if corrupt:
        # keep the header so it still "is a database", break page content
        data = data[:100] + b"\xff" * (len(data) - 100)
    return data


def make_archive(path, entries):
    """entries: list of (name, bytes) written as regular files."""
    import io
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))


def no_stage_leftovers(deploy_dir):
    return not [n for n in os.listdir(deploy_dir) if n.startswith(".rollback-stage.")]


def setup_deploy_dir(tmp):
    d = os.path.join(tmp, "deploy_dir")
    os.makedirs(d)
    with open(os.path.join(d, "docker-compose.yml"), "w") as f:
        f.write("services:\n  onepage:\n    image: onepage:latest\n")
    with open(os.path.join(d, ".env"), "w") as f:
        f.write("OP_INITIAL_ADMIN=admin1\nOP_LLM_API_KEY=super-secret\n")
    return d


def test_bad_tag_does_not_stop_container():
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        log = make_fake_docker(bindir, inspect_ok=False)
        deploy_dir = setup_deploy_dir(tmp)

        r = run_rollback(deploy_dir, bindir, "1bd6c9-typo")
        check("bad tag: rollback.sh exits non-zero", r.returncode != 0, r.stderr)
        check("bad tag: error mentions 'not found'", "not found" in r.stderr, r.stderr)

        log_content = open(log).read() if os.path.exists(log) else ""
        check("bad tag: docker compose/stop was NEVER called", "stop" not in log_content, log_content)


def test_foreign_archive_rejected_before_extraction():
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        log = make_fake_docker(bindir, inspect_ok=False)
        deploy_dir = setup_deploy_dir(tmp)

        # A foreign archive: not a bundle (no onepage-image.tar.gz), and NOT
        # a clean ./data backup (an entry escapes data/) — e.g. someone
        # accidentally hands the rollback a full-machine backup or an
        # attacker-crafted archive that would overwrite docker-compose.yml.
        evil = os.path.join(tmp, "evil.tar.gz")
        with tarfile.open(evil, "w:gz") as tf:
            import io
            payload = b"services:\n  onepage:\n    image: evil:latest\n"
            info = tarfile.TarInfo(name="docker-compose.yml")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
            payload2 = b"fake db"
            info2 = tarfile.TarInfo(name="data/onepage.db")
            info2.size = len(payload2)
            tf.addfile(info2, io.BytesIO(payload2))

        before = open(os.path.join(deploy_dir, "docker-compose.yml")).read()
        r = run_rollback(deploy_dir, bindir, evil)
        check("foreign archive: rollback.sh exits non-zero", r.returncode != 0, r.stderr)

        log_content = open(log).read() if os.path.exists(log) else ""
        check("foreign archive: docker compose/stop was NEVER called", "stop" not in log_content, log_content)

        after = open(os.path.join(deploy_dir, "docker-compose.yml")).read()
        check("foreign archive: docker-compose.yml in deploy dir untouched", before == after)
        check(
            "foreign archive: nothing extracted into deploy dir (no data/ dir created)",
            not os.path.exists(os.path.join(deploy_dir, "data")),
        )


def test_clean_data_backup_is_accepted_for_validation():
    """A backup where every entry is under data/ must pass the content
    check (script proceeds past validation to actually stop the container -
    we don't run it all the way through since that needs a live server)."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        log = make_fake_docker(bindir, inspect_ok=False)
        deploy_dir = setup_deploy_dir(tmp)

        good = os.path.join(tmp, "data-backup.tar.gz")
        make_archive(good, [("./data/onepage.db", sqlite_bytes(tmp))])

        r = run_rollback(deploy_dir, bindir, good, health_retries=1)
        # It WILL proceed past validation (stop gets called, restore happens),
        # then fail later waiting for /health (no real server) - that's fine,
        # we only assert it got past the content-validation stage.
        log_content = open(log).read() if os.path.exists(log) else ""
        check("clean data backup: passes validation and proceeds to stop", "stop" in log_content, log_content)
        check(
            "clean data backup: ./data restored in deploy dir",
            os.path.isfile(os.path.join(deploy_dir, "data", "onepage.db")),
        )
        check("clean data backup: no staging dir left behind", no_stage_leftovers(deploy_dir))


def test_backup_sh_flat_archive_restored_via_restore_sh():
    """The archive deploy/backup.sh actually produces: onepage.db, uploads/
    and runtime overlays (*.json) in the archive ROOT (tar -C view .), no
    data/ prefix. Must be accepted, restored through deploy/restore.sh
    (--archive/--dest), current ./data moved aside, uploads + overlays
    present in the new ./data."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        log = make_fake_docker(bindir, inspect_ok=False)
        deploy_dir = setup_deploy_dir(tmp)
        os.makedirs(os.path.join(deploy_dir, "data", "uploads"))
        with open(os.path.join(deploy_dir, "data", "onepage.db"), "wb") as f:
            f.write(b"live db to be moved aside")

        flat = os.path.join(tmp, "onepage-backup-20260906-000000.tar.gz")
        make_archive(flat, [
            ("./onepage.db", sqlite_bytes(tmp)),
            ("./llm-runtime.json", b"{}"),
            ("./uploads/1/index.html", b"<html></html>"),
        ])

        r = run_rollback(deploy_dir, bindir, flat, health_retries=1)
        log_content = open(log).read() if os.path.exists(log) else ""
        check("flat backup: passes validation and proceeds to stop", "stop" in log_content, log_content)
        check("flat backup: restore.sh was used (integrity_check in output)", "integrity_check: ok" in r.stdout, r.stdout + r.stderr)
        check("flat backup: restore.sh did not reject its CLI", "unknown argument" not in (r.stdout + r.stderr), r.stdout + r.stderr)
        check(
            "flat backup: ./data/onepage.db is the restored (real sqlite) db",
            open(os.path.join(deploy_dir, "data", "onepage.db"), "rb").read()[:16] == b"SQLite format 3\x00",
        )
        check("flat backup: uploads restored", os.path.isfile(os.path.join(deploy_dir, "data", "uploads", "1", "index.html")))
        check("flat backup: runtime overlay restored", os.path.isfile(os.path.join(deploy_dir, "data", "llm-runtime.json")))
        saved = [n for n in os.listdir(deploy_dir) if n.startswith("data.before-rollback-")]
        check("flat backup: previous ./data moved aside", len(saved) == 1, str(os.listdir(deploy_dir)))
        check("flat backup: no staging dir left behind", no_stage_leftovers(deploy_dir))
        check("flat backup: order is prepare -> stop -> up", log_content.find("stop") < log_content.find("up -d"), log_content)


def test_corrupt_backup_rejected_before_stop():
    """integrity_check runs in staging BEFORE the container is stopped: a
    corrupt backup exits non-zero, never calls stop, leaves live ./data and
    the deploy dir untouched, and cleans its staging dir."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        log = make_fake_docker(bindir, inspect_ok=False)
        deploy_dir = setup_deploy_dir(tmp)
        os.makedirs(os.path.join(deploy_dir, "data"))
        with open(os.path.join(deploy_dir, "data", "onepage.db"), "wb") as f:
            f.write(b"live db")

        bad = os.path.join(tmp, "onepage-backup-corrupt.tar.gz")
        make_archive(bad, [("./onepage.db", sqlite_bytes(tmp, "bad.db", corrupt=True))])

        r = run_rollback(deploy_dir, bindir, bad, health_retries=1)
        log_content = open(log).read() if os.path.exists(log) else ""
        check("corrupt backup: rollback.sh exits non-zero", r.returncode != 0)
        check("corrupt backup: docker compose/stop was NEVER called", "stop" not in log_content, log_content)
        check(
            "corrupt backup: live ./data untouched",
            open(os.path.join(deploy_dir, "data", "onepage.db"), "rb").read() == b"live db",
        )
        check("corrupt backup: nothing moved aside", not [n for n in os.listdir(deploy_dir) if n.startswith("data.before-rollback-")])
        check("corrupt backup: no staging dir left behind", no_stage_leftovers(deploy_dir))


def test_flat_archive_with_deploy_files_rejected():
    """onepage.db in the root does not make an archive trustworthy if it also
    carries docker-compose.yml / .env / *.sh — reject before any extraction."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        log = make_fake_docker(bindir, inspect_ok=False)
        deploy_dir = setup_deploy_dir(tmp)

        evil = os.path.join(tmp, "evil-flat.tar.gz")
        make_archive(evil, [
            ("./onepage.db", sqlite_bytes(tmp)),
            ("./.env", b"OP_LLM_API_KEY=stolen\n"),
        ])
        before = open(os.path.join(deploy_dir, ".env")).read()
        r = run_rollback(deploy_dir, bindir, evil)
        log_content = open(log).read() if os.path.exists(log) else ""
        check("evil flat archive: rollback.sh exits non-zero", r.returncode != 0)
        check("evil flat archive: docker compose/stop was NEVER called", "stop" not in log_content, log_content)
        check("evil flat archive: .env in deploy dir untouched", before == open(os.path.join(deploy_dir, ".env")).read())
        check("evil flat archive: no data/ created, no staging left", not os.path.exists(os.path.join(deploy_dir, "data")) and no_stage_leftovers(deploy_dir))

        trav = os.path.join(tmp, "traversal.tar.gz")
        make_archive(trav, [("./onepage.db", sqlite_bytes(tmp, "trav.db")), ("../escape.txt", b"x")])
        r = run_rollback(deploy_dir, bindir, trav)
        log_content = open(log).read() if os.path.exists(log) else ""
        check("traversal archive: rejected", r.returncode != 0 and ".." in r.stderr, r.stderr)
        check("traversal archive: no stop", "stop" not in log_content, log_content)
        check("traversal archive: nothing escaped to parent", not os.path.exists(os.path.join(tmp, "escape.txt")))


if __name__ == "__main__":
    test_bad_tag_does_not_stop_container()
    test_foreign_archive_rejected_before_extraction()
    test_clean_data_backup_is_accepted_for_validation()
    test_backup_sh_flat_archive_restored_via_restore_sh()
    test_corrupt_backup_rejected_before_stop()
    test_flat_archive_with_deploy_files_rejected()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
