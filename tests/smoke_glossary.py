"""Smoke test for the platform glossary (H2): model, reach-scoped read API,
admin write API (permission + audit), and its wiring into
`nl2sql.build_intent_prompt`'s `extra_glossary` parameter.
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-glossary-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import nl2sql  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def make_bundle(name):
    m = {
        "name": name,
        "version": "1.0",
        "entrypoint": {"module": "gen.main"},
        "outputs": {"html": True, "slots": []},
        "engine": "browser",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


def register(c, gen_name, title):
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle(gen_name), "application/zip")})
    assert r.status_code == 201, f"register {gen_name}: {r.status_code} {r.text[:400]}"
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": title, "generator_id": gid})
    assert r.status_code == 201, f"create {title}: {r.status_code} {r.text[:300]}"
    return gid, r.json()["id"]


with TestClient(app) as c:
    from app.database import SessionLocal
    from app.models import User

    _db = SessionLocal()
    _db.add(User(person_id="anna", role="superuser"))
    _db.commit()
    _db.close()

    login(c, "admin1")
    gid1, dash1 = register(c, "gen-one", "Gen One Dashboard")
    gid2, dash2 = register(c, "gen-two", "Gen Two Dashboard")

    # --- write permissions -------------------------------------------------
    login(c, "anna")
    r = c.post("/api/admin/glossary", json={"term": "SL", "definition": "Service Level"})
    check("superuser cannot write glossary (403)", r.status_code == 403, str(r.status_code))

    login(c, "admin1")
    r = c.post("/api/admin/glossary", json={"term": "SL", "definition": "Service Level, company-wide"})
    check("admin creates company-wide term -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    term_sl = r.json()
    check("company-wide term has null generator_id", term_sl["generator_id"] is None, str(term_sl))

    r = c.post("/api/admin/glossary", json={"term": "hub", "definition": "distribution node",
                                            "generator_id": gid1})
    check("admin creates generator-scoped term -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    term_hub = r.json()

    # --- validation ----------------------------------------------------
    r = c.post("/api/admin/glossary", json={"term": "", "definition": "x"})
    check("blank term -> 422", r.status_code == 422, str(r.status_code))

    r = c.post("/api/admin/glossary", json={"term": "nowhere", "definition": "x", "generator_id": 999999})
    check("unknown generator -> 404", r.status_code == 404, str(r.status_code))

    # --- uniqueness, including the NULL-generator_id case sqlite won't catch
    r = c.post("/api/admin/glossary", json={"term": "sl", "definition": "duplicate, different case"})
    check("duplicate company-wide term (case-insensitive) -> 409", r.status_code == 409, str(r.status_code))

    r = c.post("/api/admin/glossary", json={"term": "SL", "definition": "same term, different generator scope",
                                            "generator_id": gid1})
    check("same term, different scope -> 201 (not a collision)", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    term_sl_gen1 = r.json()

    # --- update / delete -------------------------------------------------
    r = c.put(f"/api/admin/glossary/{term_hub['id']}",
             json={"term": "hub", "definition": "distribution node of the network", "generator_id": gid1})
    check("update term -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("update reflects new definition", r.json()["definition"] == "distribution node of the network", str(r.json()))

    r = c.delete(f"/api/admin/glossary/{term_sl_gen1['id']}")
    check("delete term -> 200", r.status_code == 200, str(r.status_code))
    r = c.delete(f"/api/admin/glossary/{term_sl_gen1['id']}")
    check("delete again -> 404", r.status_code == 404, str(r.status_code))

    # --- audit log ---------------------------------------------------------
    from app.models import AuditLog
    db = SessionLocal()
    actions = {a.action for a in db.query(AuditLog).filter(AuditLog.resource_type == "glossary_term").all()}
    db.close()
    check("writes are audited (create/update/delete)",
         {"glossary_create", "glossary_update", "glossary_delete"} <= actions, str(actions))

    # --- read reach: company-wide + reachable generator scope --------------
    login(c, "anna")  # superuser, no dashboards of her own yet
    r = c.get("/api/glossary")
    check("anon-reach read -> 200", r.status_code == 200, str(r.status_code))
    terms = {t["term"]: t for t in r.json()["terms"]}
    check("superuser sees company-wide term without any generator reach",
         "SL" in terms, str(terms))
    check("superuser does NOT see gen1-scoped term (no reach)",
         "hub" not in terms, str(terms))

    login(c, "admin1")  # owns dash1 (gen1) and dash2 (gen2)
    r = c.get("/api/glossary")
    terms = {t["term"]: t for t in r.json()["terms"]}
    check("admin (owns dash of gen1) sees gen1-scoped term",
         "hub" in terms, str(terms))

    r = c.get(f"/api/glossary?generator_id={gid1}")
    check("scoped read (own generator) -> 200", r.status_code == 200, str(r.status_code))
    terms = {t["term"]: t for t in r.json()["terms"]}
    check("scoped read returns company-wide + gen1 terms only",
         "SL" in terms and "hub" in terms, str(terms))

    r = c.get("/api/glossary?generator_id=999999")
    check("scoped read of unreachable generator -> 404", r.status_code == 404, str(r.status_code))

    # --- nl2sql wiring: extra_glossary merges ahead of the semantics one ---
    sem = {"kpis": {"k1": {"name": "K1", "definition": "test kpi", "dims": []}},
          "glossary": {"hub": "semantics definition, should not win"}}
    prompt = nl2sql.build_intent_prompt(sem, ["k1"], "a question about the hub",
                                        extra_glossary={"hub": "platform definition", "SL": "Service Level"})
    lines = prompt.splitlines()
    hub_line = next((l for l in lines if l.startswith("- hub:")), "")
    check("extra_glossary entry present and wins over semantics glossary for the same term",
         "platform definition" in hub_line and "should not win" not in hub_line, hub_line)
    check("platform-only term also present", any(l.startswith("- SL:") for l in lines), prompt)

    # default (no extra_glossary) still works untouched
    prompt2 = nl2sql.build_intent_prompt(sem, ["k1"], "a question")
    check("build_intent_prompt still works with no extra_glossary",
         any("semantics definition" in l for l in prompt2.splitlines()), prompt2)

    # cap still respected when combined list is large
    big_extra = {f"term{i}": f"def{i}" for i in range(30)}
    prompt3 = nl2sql.build_intent_prompt({"kpis": {"k1": {"name": "K1", "dims": []}}, "glossary": {}},
                                         ["k1"], "a question", extra_glossary=big_extra)
    glossary_lines = [l for l in prompt3.splitlines() if l.startswith("- term")]
    check(f"combined glossary still capped at MAX_INTENT_GLOSSARY ({nl2sql.MAX_INTENT_GLOSSARY})",
         len(glossary_lines) <= nl2sql.MAX_INTENT_GLOSSARY, str(len(glossary_lines)))


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("All glossary smoke checks passed.")
