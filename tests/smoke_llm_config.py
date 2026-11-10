"""Admin LLM runtime config (/api/admin/llm + app/services/runtime_config.py):
gates, write-only keys, overlay semantics, effect on /api/explore/llm-config."""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-llm-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_EXPLORE_ENABLED": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_LLM_PROVIDER": "ollama",
    "OP_LLM_MODEL": "gemma3:4b",
})
sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


_ctx = TestClient(app)
client = _ctx.__enter__()

client.post("/dev-login", data={"person_id": "admin1"})
r = client.post("/api/admin/users", json={"person_id": "plainuser", "role": "visitor"})
assert r.status_code == 200, r.text

# ── gates ───────────────────────────────────────────────────────────────
r = client.post("/dev-login", data={"person_id": "plainuser"}, follow_redirects=False)
assert r.status_code == 302
r = client.get("/api/admin/llm")
check("plain user: GET denied", r.status_code in (401, 403), str(r.status_code))
r = client.put("/api/admin/llm", json={"llm_provider": "deepseek"})
check("plain user: PUT denied", r.status_code in (401, 403), str(r.status_code))

# ── env defaults visible ───────────────────────────────────────────────
client.post("/dev-login", data={"person_id": "admin1"})
r = client.get("/api/admin/llm")
check("admin: GET 200", r.status_code == 200, str(r.status_code))
eff = r.json()["effective"]
check("effective mirrors env defaults", eff["provider"] == "ollama" and eff["model"] == "gemma3:4b", str(eff))
check("no raw key field in the payload (only *_set flags)",
      '"llm_api_key":' not in r.text and '"llm_browser_key":' not in r.text, r.text[:200])

# ── switch to deepseek + browser key ───────────────────────────────────
r = client.put("/api/admin/llm", json={
    "llm_provider": "deepseek", "llm_browser": True, "llm_browser_key": "sk-SECRET-123",
})
check("PUT: 200", r.status_code == 200, r.text[:200])
check("PUT: changed names only, no values",
      set(r.json()["changed"]) == {"llm_provider", "llm_browser", "llm_browser_key"}
      and "sk-SECRET" not in r.text, r.text[:200])

r = client.get("/api/admin/llm")
eff = r.json()["effective"]
check("effective: deepseek + browser + key_set",
      eff["provider"] == "deepseek" and eff["browser"] is True and eff["browser_key_set"] is True, str(eff))
check("GET never echoes the key", "sk-SECRET" not in r.text)

# The page-facing config follows without a restart.
r = client.get("/api/explore/llm-config")
body = r.json()
check("explore llm-config: provider switched at runtime", body.get("provider") == "deepseek", str(body))

# invalid provider rejected
r = client.put("/api/admin/llm", json={"llm_provider": "gpt5"})
check("invalid provider → 400", r.status_code == 400, str(r.status_code))

# ── audit carries names, never the key ─────────────────────────────────
r = client.get("/api/admin/audit-log", params={"limit": 10})
entries = [e for e in r.json() if e["action"] == "llm_config_update"]
check("audited", len(entries) >= 1)
check("audit detail: names only", "sk-SECRET" not in json.dumps(entries), json.dumps(entries)[:200])

# ── file survives, is 0600, and reset restores env ─────────────────────
from app.config import settings  # noqa: E402
path = settings.abs_db_path.parent / "llm-runtime.json"
check("overlay file exists next to the DB", path.exists(), str(path))
check("overlay file is 0600", oct(path.stat().st_mode & 0o777) == "0o600", oct(path.stat().st_mode & 0o777))
check("key IS in the file (that's where it lives)", "sk-SECRET-123" in path.read_text())

r = client.put("/api/admin/llm", json={"reset": ["llm_provider", "llm_browser", "llm_browser_key"]})
check("reset: 200", r.status_code == 200, r.text[:200])
r = client.get("/api/admin/llm")
eff = r.json()["effective"]
check("reset restores env defaults", eff["provider"] == "ollama" and eff["browser_key_set"] is False, str(eff))

# ── DeepSeek allowlist ─────────────────────────────────────────────────
r = client.post("/api/admin/users", json={"person_id": "admin2", "role": "administrator"})
assert r.status_code == 200, r.text

# With OP_LLM_ACCESS_ADMINS unset any administrator may assign.
r = client.get("/api/admin/llm/access")
check("access list: 200, can_assign for admin (no env narrowing)",
      r.status_code == 200 and r.json()["can_assign"] is True, r.text[:200])
r = client.put("/api/admin/llm/access/plainuser")
check("grant: 200", r.status_code == 200, r.text[:200])
r = client.put("/api/admin/llm/access/plainuser")
check("grant twice: idempotent", r.status_code == 200)
r = client.put("/api/admin/llm/access/ghost-nobody")
check("grant to unknown user → 404", r.status_code == 404, str(r.status_code))
r = client.get("/api/admin/llm/access")
items = r.json()["items"]
check("list shows the grant with granted_by",
      len(items) == 1 and items[0]["person_id"] == "plainuser" and items[0]["granted_by"] == "admin1",
      str(items))

# Narrow assignment to one person_id — the other administrator loses the right.
from app.services import llm_access, runtime_config  # noqa: E402
settings.llm_access_admins = "admin1"
try:
    client.post("/dev-login", data={"person_id": "admin2"})
    r = client.get("/api/admin/llm/access")
    check("narrowed: other admin sees can_assign=False", r.json()["can_assign"] is False, r.text[:200])
    r = client.put("/api/admin/llm/access/admin2")
    check("narrowed: other admin cannot grant (403)", r.status_code == 403, str(r.status_code))
    r = client.delete("/api/admin/llm/access/plainuser")
    check("narrowed: other admin cannot revoke (403)", r.status_code == 403, str(r.status_code))
    client.post("/dev-login", data={"person_id": "admin1"})
    r = client.put("/api/admin/llm/access/admin2")
    check("narrowed: designated admin still grants", r.status_code == 200, str(r.status_code))
finally:
    settings.llm_access_admins = ""

# allowed(): ollama → everyone; deepseek → rows only.
from app.database import SessionLocal  # noqa: E402
from app.models import User as UserModel  # noqa: E402
db = SessionLocal()
u_plain = db.query(UserModel).filter(UserModel.person_id == "plainuser").first()
u_admin = db.query(UserModel).filter(UserModel.person_id == "admin1").first()
check("allowed: ollama → True for everyone", llm_access.allowed(db, u_admin) is True)
client.put("/api/admin/llm", json={"llm_provider": "deepseek"})
runtime_config.ensure_fresh(force=True)
check("allowed: deepseek → True with a row", llm_access.allowed(db, u_plain) is True)
check("allowed: deepseek → False without a row (even administrator)",
      llm_access.allowed(db, u_admin) is False)
client.put("/api/admin/llm", json={"reset": ["llm_provider"]})
db.close()

r = client.delete("/api/admin/llm/access/plainuser")
check("revoke: 200", r.status_code == 200)
r = client.get("/api/admin/llm/access")
check("revoked user gone from the list",
      all(i["person_id"] != "plainuser" for i in r.json()["items"]))

r = client.get("/api/admin/audit-log", params={"limit": 20})
actions = {e["action"] for e in r.json()}
check("grants and revokes audited", {"llm_access_grant", "llm_access_revoke"} <= actions, str(actions))

# ── the gate on /explore routes ────────────────────────────────────────
client.put("/api/admin/llm", json={
    "llm_provider": "deepseek", "llm_browser": True,
    "llm_browser_key": "sk-X", "llm_api_key": "sk-Y",
})
runtime_config.ensure_fresh(force=True)

client.post("/dev-login", data={"person_id": "plainuser"})
r = client.get("/api/explore/llm-config")
cfg = r.json()
check("gate: non-listed user sees no model anywhere (browser/server False, no key)",
      cfg["provider"] == "deepseek" and cfg["browser"] is False
      and cfg["server"] is False and cfg["key"] is None, str(cfg))
r = client.post("/api/explore/semantic-sql/prepare",
                json={"question": "how many in total?", "generator_id": 1})
check("gate: semantic-sql/prepare → 403 for non-listed", r.status_code == 403, str(r.status_code))

client.post("/dev-login", data={"person_id": "admin1"})
client.put("/api/admin/llm/access/plainuser")
client.post("/dev-login", data={"person_id": "plainuser"})
r = client.get("/api/explore/llm-config")
cfg = r.json()
check("gate: listed user gets browser mode and server flag back",
      cfg["browser"] is True and cfg["server"] is True, str(cfg))
r = client.post("/api/explore/semantic-sql/prepare",
                json={"question": "how many in total?", "generator_id": 1})
check("gate: listed user passes the gate (fails later on reach, not 403)",
      r.status_code != 403, str(r.status_code))

client.post("/dev-login", data={"person_id": "admin1"})
client.delete("/api/admin/llm/access/plainuser")
client.put("/api/admin/llm", json={"reset": ["llm_provider", "llm_browser",
                                             "llm_browser_key", "llm_api_key"]})
runtime_config.ensure_fresh(force=True)
client.post("/dev-login", data={"person_id": "plainuser"})
r = client.get("/api/explore/llm-config")
check("gate: back on ollama nothing is restricted",
      r.json()["provider"] == "ollama", r.text[:120])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES")
    sys.exit(1)
print("ALL PASS")
