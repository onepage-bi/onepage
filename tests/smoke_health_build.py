"""Regression: /health reports a build sha and start time, and OP_BUILD_SHA
(and the app/BUILD file it usually comes from) actually control it.

Also asserts deploy/make-patch.sh cannot silently drop app/BUILD from a patch
— a patch without it would leave the server reporting the sha it had BEFORE
the patch, which defeats the entire point of this feature.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-health-build-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_SAML_SP_BASE_URL": "http://testserver",
    "OP_BUILD_SHA": "deadbee",
})

REPO = os.getcwd()
sys.path.insert(0, REPO)

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def test_health_reports_build_from_env():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/health")
        check("GET /health -> 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check("status == ok", body.get("status") == "ok", str(body))
        check("build == OP_BUILD_SHA value", body.get("build") == "deadbee", str(body))
        check("started_at present", bool(body.get("started_at")), str(body))

        deep = client.get("/health?deep=1")
        deep_body = deep.json()
        check("deep /health also carries build", deep_body.get("build") == "deadbee", str(deep_body))
        check("deep /health also carries started_at", bool(deep_body.get("started_at")), str(deep_body))


def test_config_falls_back_to_build_file():
    """Without OP_BUILD_SHA, app/BUILD (if present) wins; the repo's real
    app/BUILD (gitignored) may or may not exist on this checkout, so this only
    pins the *env-var-wins* half directly usable without touching real files:
    OP_BUILD_SHA must override whatever app/BUILD says."""
    from importlib import reload
    import app.config as config_mod

    os.environ["OP_BUILD_SHA"] = "cafefeed"
    reload(config_mod)
    check("Settings() honors OP_BUILD_SHA override",
          config_mod.settings.build_sha == "cafefeed",
          config_mod.settings.build_sha)
    os.environ["OP_BUILD_SHA"] = "deadbee"
    reload(config_mod)


def test_make_patch_includes_build_stamp():
    patch_sh = os.path.join(REPO, "deploy", "make-patch.sh")
    with open(patch_sh, encoding="utf-8") as f:
        src = f.read()
    check("make-patch.sh writes app/BUILD into the patch",
          "files/app/BUILD" in src and 'echo "$TO_SHORT"' in src,
          "expected a step writing $OUT/files/app/BUILD from $TO_SHORT")
    check("make-patch.sh adds app/BUILD to the MANIFEST",
          'FILES+=("app/BUILD")' in src,
          "app/BUILD must be listed in MANIFEST or apply.sh will not copy it")


def test_make_bundle_writes_build_stamp():
    bundle_sh = os.path.join(REPO, "deploy", "make-bundle.sh")
    with open(bundle_sh, encoding="utf-8") as f:
        src = f.read()
    check("make-bundle.sh writes app/BUILD before the docker build",
          "app/BUILD" in src and "rev-parse --short HEAD" in src)


def test_rollback_script_exists_and_is_executable():
    rollback_sh = os.path.join(REPO, "deploy", "rollback.sh")
    check("deploy/rollback.sh exists", os.path.isfile(rollback_sh))
    check("deploy/rollback.sh is executable", os.access(rollback_sh, os.X_OK))


if __name__ == "__main__":
    test_health_reports_build_from_env()
    test_config_falls_back_to_build_file()
    test_make_patch_includes_build_stamp()
    test_make_bundle_writes_build_stamp()
    test_rollback_script_exists_and_is_executable()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL PASS")
