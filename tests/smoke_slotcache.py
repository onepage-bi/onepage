"""Regression: a republished dashboard must never serve the PREVIOUS data.

Field report (22.07.2026, DELTA agent pilot): a generator run published new data
at 14:37, the page kept showing the 14:32 numbers, and "Show new report"

(location.reload) changed nothing. Cause: slot urls carry no version, and the
response said `max-age=300, must-revalidate` — which does NOT revalidate while
the response is still fresh (RFC 9111 §5.2.2.2), so for 300 s the browser
answered the data fetch from its own cache without asking the server. Exactly
five minutes, exactly the gap observed.

The fix is key rotation, not a shorter window: slots_json carries `v`
(storage.slot_fp), the page appends `?v=`, and only a MATCHING v is cacheable.
This test pins both halves — a stale/absent v must be uncacheable, and the
bytes served must always be the current ones.
"""
import json
import os
import re
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-slotcache-")
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

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


HTML = b"<html><head><title>delta</title></head><body><h1>report</h1></body></html>"
DATA_V1 = b"row_id,gp_login\n1,u10000009\n"   # the 14:32 data
DATA_V2 = b"row_id,gp_login\n2,u10000009\n"   # the 14:37 data


def slots_of(c, dash_id):
    """The slot list exactly as the page hands it to viewer.js."""
    page = c.get(f"/d/{dash_id}").text
    m = re.search(r"const SLOTS = (\[.*?\]);", page, re.S)
    assert m, "slots_json not found in viewer page"
    return json.loads(m.group(1))


with TestClient(app) as c:
    c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)

    r = c.post("/api/dashboards",
               data={"title": "delta", "description": ""},
               files={"html_file": ("delta.html", HTML, "text/html"),
                      "data_file_0": ("delta.csv", DATA_V1, "text/csv")},
               follow_redirects=False)
    check("dashboard created", r.status_code == 303, f"{r.status_code} {r.text[:200]}")
    dash_id = r.headers["location"].rsplit("/", 1)[1]

    # --- the page hands the client a fingerprint -------------------------
    slots = slots_of(c, dash_id)
    check("slots_json carries a slot", len(slots) == 1, str(slots))
    v1 = slots[0].get("v")
    check("slots_json carries v", bool(v1), str(slots))

    url = f"/render/{dash_id}/files/0"

    # --- only a matching v is cacheable ---------------------------------
    r = c.get(url)
    check("no v -> uncacheable", "no-store" in r.headers["cache-control"], r.headers["cache-control"])
    check("no v -> current bytes", r.content == DATA_V1)

    r = c.get(url, params={"v": v1})
    check("matching v -> immutable", "immutable" in r.headers["cache-control"], r.headers["cache-control"])
    check("matching v -> current bytes", r.content == DATA_V1)

    r = c.get(url, params={"v": "deadbeef-1"})
    check("stale v -> uncacheable", "no-store" in r.headers["cache-control"], r.headers["cache-control"])
    check("stale v -> current bytes (self-heal)", r.content == DATA_V1)

    # --- THE FIELD BUG: republish, then read as the reloaded page would --
    r = c.put(f"/api/dashboards/{dash_id}/files/0",
              files={"file": ("delta.csv", DATA_V2, "text/csv")})
    check("slot replaced", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    v2 = slots_of(c, dash_id)[0].get("v")
    check("republish rotates v", v2 != v1, f"{v1} -> {v2}")

    # What location.reload() now issues: a url the browser has never seen.
    r = c.get(url, params={"v": v2})
    check("reloaded page sees NEW data", r.content == DATA_V2, r.content[:60])

    # The url the browser DID cache must not be reusable for the old bytes.
    r = c.get(url, params={"v": v1})
    check("previously-cached url is uncacheable", "no-store" in r.headers["cache-control"],
          r.headers["cache-control"])
    check("previously-cached url yields NEW data", r.content == DATA_V2, r.content[:60])

    # --- relative assets: url is author-written, so it cannot rotate -----
    r = c.get(f"/render/{dash_id}/delta.csv")
    check("asset revalidates every time", r.headers["cache-control"] == "private, no-cache",
          r.headers["cache-control"])
    check("asset serves current bytes", r.content == DATA_V2)
    r = c.get(f"/render/{dash_id}/delta.csv", headers={"If-None-Match": r.headers["etag"]})
    check("asset 304 still works", r.status_code == 304, str(r.status_code))

# --- the client half must not silently revert ---------------------------
viewer = open("static/js/viewer.js", encoding="utf-8").read()
share = open("templates/pages/share.html", encoding="utf-8").read()
check("viewer.js appends ?v=", "?v=${slot.v}" in viewer)
check("share.html appends ?v=", "?v=${slot.v}" in share)

print()
print(f"{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURES: ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
