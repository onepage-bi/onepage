"""A report as one file: the document together with its data.

Background (2026-10-12, owner): "can I pull the report out with its data? right now it
does not export in full". The cause is not the export but what it lacks:
"Download current version" serves `dashboard.html`, while an interactive
report's data lives in slots and arrives through the `op_ready` -> `op_file` bridge only
at view time. The downloaded file opened empty.

This test pins exactly the places where such a build breaks SILENTLY, i.e.
serves a file that opens, but without numbers:

  * a data slot is embedded, and THE SAME bytes are recovered from the file;
  * gzip is left to the browser, brotli is decoded by us (the browser has nothing
    to decode it with, so the slot would reach the report as garbage);
  * the snapshot is embedded in the document even when the server is configured to serve it
    as an external `snapshot.js` (a file on disk has no relative address);
  * a slot the document itself references is an ASSET: it is pasted into the
    markup, not sent as a message;
  * a report with row-level security (ADR-18) has no export at all, neither endpoint
    nor button, and that is about the artifact itself, not about rights.

OP_SNAPSHOT_EXTERNAL is turned ON here on purpose: otherwise the "force-embed the
snapshot" branch never runs.
"""
import base64
import json
import os
import re
import sys
import gzip
import tempfile

TMP = tempfile.mkdtemp(prefix="op-standalone-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_SNAPSHOT_EXTERNAL": "true",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient      # noqa: E402
from app.main import app                       # noqa: E402
from app.database import SessionLocal          # noqa: E402
from app.models import AuditLog, Dashboard, User  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


INTERACTIVE = (b"<html><head><title>sl</title></head><body>"
               b"<input type=\"file\"><div id=\"out\"></div></body></html>")
WITH_ASSET = (b"<html><head><title>sl</title><script src=\"helper.js\"></script>"
              b"</head><body><input type=\"file\"><input type=\"file\">"
              b"</body></html>")
SNAPSHOT_HTML = (b"<html><head><title>snap</title>"
                 b"<script id=\"bootstrap-snapshot\" type=\"application/json\">{}</script>"
                 b"</head><body><h1>snap</h1></body></html>")

DATA = b"point,sales\nZR-01,163548\n"
ASSET_JS = b"window.OP_HELPER = 1; // </script> inside the asset must not escape\n"


def slots_in(doc: str):
    """The slot list from the bridge: what actually reaches the report."""
    m = re.search(r"var SLOTS = (\[.*?\]);", doc, re.S)
    return json.loads(m.group(1)) if m else []


def create(c, html, extra_files=None):
    files = {"html_file": ("r.html", html, "text/html")}
    files.update(extra_files or {})
    r = c.post("/api/dashboards", data={"title": "r", "description": ""},
               files=files, follow_redirects=False)
    assert r.status_code == 303, (r.status_code, r.text[:300])
    return r.headers["location"].rsplit("/", 1)[1]


with TestClient(app) as c:
    c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)

    # --- 1. interactive report with a data slot -------------------------
    dash = create(c, INTERACTIVE,
                  {"data_file_0": ("sl.csv", DATA, "text/csv")})
    r = c.get(f"/api/dashboards/{dash}/standalone")
    check("export served", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("it is an attachment", "attachment" in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition", ""))
    doc = r.text
    check("bridge in the document", "op_file" in doc and "var SLOTS" in doc)
    slots = slots_in(doc)
    check("one slot", len(slots) == 1, str([s.get("name") for s in slots]))
    check("slot name preserved", slots and slots[0]["name"] == "sl.csv",
          str(slots[:1]))
    check("bytes are recovered one-to-one",
          bool(slots) and base64.b64decode(slots[0]["b64"]) == DATA)
    check("uncompressed slot marked identity", bool(slots) and slots[0]["enc"] == "identity",
          str(slots[:1]))
    # The bridge sits BEFORE </body>: after it the browser would simply not run it.
    check("bridge before </body>", doc.index("var SLOTS") < doc.lower().rindex("</body>"))

    # --- 2. gzip is left to the browser ---------------------------------
    gz = gzip.compress(DATA)
    dash_gz = create(c, INTERACTIVE, {"data_file_0": ("sl.csv.gz", gz, "application/gzip")})
    slots = slots_in(c.get(f"/api/dashboards/{dash_gz}/standalone").text)
    check("gzip slot marked gzip", bool(slots) and slots[0]["enc"] == "gzip",
          str(slots[:1]))
    check("the compressed bytes are what is embedded",
          bool(slots) and base64.b64decode(slots[0]["b64"]) == gz)
    # The file has a decompressor; otherwise the report would get garbage and not say so.
    check("the bridge has DecompressionStream",
          "DecompressionStream" in c.get(f"/api/dashboards/{dash_gz}/standalone").text)

    # --- 3. a slot the document references is an asset ------------------
    dash_a = create(c, WITH_ASSET,
                    {"data_file_0": ("helper.js", ASSET_JS, "text/javascript"),
                     "data_file_1": ("sl.csv", DATA, "text/csv")})
    doc = c.get(f"/api/dashboards/{dash_a}/standalone").text
    check("asset pasted into the document", "window.OP_HELPER = 1" in doc)
    check("no reference to the external file left",
          'src="helper.js"' not in doc and "src='helper.js'" not in doc)
    check("</script> inside the asset is neutralised", "<\\/script>" in doc)
    names = [s["name"] for s in slots_in(doc)]
    check("asset NOT sent as a message", "helper.js" not in names, str(names))
    check("data sent as a message", "sl.csv" in names, str(names))

    # --- 4. snapshot embedded although the server is set to external -----
    dash_s = create(c, SNAPSHOT_HTML)
    snap = json.dumps({"rows": [{"point": "ZR-01"}]}).encode()
    rs = c.put(f"/api/dashboards/{dash_s}/snapshot",
               files={"file": ("snapshot.json", snap, "application/json")})
    check("snapshot saved", rs.status_code == 200, f"{rs.status_code} {rs.text[:200]}")
    served = c.get(f"/render/{dash_s}/").text
    check("on the platform the snapshot is external (mode on)",
          "snapshot.js?v=" in served, served[:300])
    doc = c.get(f"/api/dashboards/{dash_s}/standalone").text
    check("no external snapshot reference in the file", "snapshot.js?v=" not in doc)
    check("snapshot inline in the file", "ZR-01" in doc)

    # --- 5. report without slots: a file, no bridge ---------------------
    dash_n = create(c, b"<html><body><h1>plain</h1></body></html>")
    r = c.get(f"/api/dashboards/{dash_n}/standalone")
    check("report without slots served", r.status_code == 200, str(r.status_code))
    check("no bridge, nothing to embed", "var SLOTS" not in r.text)

    # --- 6. RLS: neither endpoint nor button ----------------------------
    db = SessionLocal()
    d = db.query(Dashboard).filter(Dashboard.id == dash).first()
    d.rls_json = json.dumps({"slots": {}})
    db.commit()
    db.close()
    r = c.get(f"/api/dashboards/{dash}/standalone")
    check("an RLS report is not exported as one file", r.status_code == 409, str(r.status_code))
    check("the reason is spelled out", "row-level security" in r.text, r.text[:200])
    page = c.get(f"/d/{dash}").text
    check("no button on the RLS report page", "btn-download-standalone" not in page)
    page_ok = c.get(f"/d/{dash_gz}").text
    check("a regular report has the button", "btn-download-standalone" in page_ok)

    # --- 7. same right as for the source --------------------------------
    db = SessionLocal()
    db.add(User(person_id="stranger1", display_name="s", role="visitor", is_active=True))
    db.commit()
    db.close()
    with TestClient(app) as other:
        other.post("/dev-login", data={"person_id": "stranger1"}, follow_redirects=False)
        r = other.get(f"/api/dashboards/{dash_gz}/standalone")
        check("a stranger is refused", r.status_code in (403, 404), str(r.status_code))

    # --- 8. audit: who took the report out with its data ---------------
    db = SessionLocal()
    rows = (db.query(AuditLog)
              .filter(AuditLog.action == "dashboard_standalone_export").all())
    check("the export reached the audit log", len(rows) >= 1, str(len(rows)))
    detail = json.loads(rows[0].detail) if rows and rows[0].detail else {}
    check("the audit has size and slots", "bytes" in detail and "slots" in detail, str(detail)[:200])
    # Audit rule: not a single value from the data.
    check("no data values in the audit", "163548" not in json.dumps(detail),
          str(detail)[:200])
    db.close()

    # --- 8b. console path: the same build, but its own output ------
    # The endpoint and the script diverge not in logic but in the details around it:
    # audit entries became structures while the script kept joining them as a string and
    # crashed on the very first export. The endpoint test did not see it.
    from tools import standalone_export                      # noqa: E402
    cli_out = os.path.join(TMP, "cli.html")
    code = standalone_export.main([dash_gz, "--out", cli_out])
    check("script ran", code == 0, str(code))
    check("script wrote the file",
          os.path.isfile(cli_out) and os.path.getsize(cli_out) > 0)
    check("script and endpoint produce the same thing",
          open(cli_out, "rb").read() == c.get(
              f"/api/dashboards/{dash_gz}/standalone").content)

# --- 9. the bridge must be IN THE COMMIT ----------------------------------
# It used to live in app/services/data/, and the `data/` rule in .gitignore catches such
# a directory at any depth: `git add -A` silently skipped it, the patch went to the
# stand without the file, and the button answered 500. The tests did not see it: the file was
# on disk. So we check not presence but whether it is TRACKED.
import subprocess  # noqa: E402

BRIDGE = "app/services/standalone_bridge.js"
check("bridge on disk", os.path.isfile(BRIDGE))
try:
    # Ask git directly instead of guessing from the error text: this used to
    # look for the substring "not in a git repository", but git says "not a git
    # repository", and an exported tree (the public slice, an unpacked
    # archive) gave a FALSE failure ("file not tracked" instead of "nothing to
    # track here"). The check is about a missed commit, and outside a repository
    # it has nothing to check.
    inside_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=20).stdout.strip() == "true"
    if inside_repo:
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", BRIDGE],
                                 capture_output=True, text=True, timeout=20)
        check("bridge is tracked by git", tracked.returncode == 0, tracked.stderr.strip())
    else:
        print("SKIP bridge is tracked by git (not a repository)")
except (OSError, subprocess.SubprocessError) as e:
    print(f"SKIP bridge is tracked by git ({e})")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
    sys.exit(1)
print("standalone: all checks passed")
